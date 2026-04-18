package dps28;

import wyvern.client.Client;
import wyvern.client.GameWindow;
import wyvern.client.ServerOutput;

import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.text.*;
import java.io.BufferedWriter;
import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class DPSAgent {

    private static final Pattern DAMAGE_PATTERN =
        Pattern.compile("for\\s+(\\d+)\\s+damage", Pattern.CASE_INSENSITIVE);
    private static final Pattern KILL_PATTERN =
        Pattern.compile("(?:You killed |You destroy |is destroyed|is dead)", Pattern.CASE_INSENSITIVE);

    private static BufferedWriter logWriter;
    private static StyledDocument activeDocument;
    private static DocumentListener activeListener;
    private static Object statProxy;
    private static Object statModel;
    private static volatile boolean active = false;
    private static Thread watchThread;

    // HP tracking
    private static volatile int lastHp = -1;

    // Pending incoming message — red text captured from ServerOutput, consumed by HP tracker
    private static volatile String pendingIncomingMsg = null;
    private static volatile long pendingIncomingTs = 0;

    public static void agentmain(String agentArgs, Instrumentation inst) {
        String logPath = agentArgs != null ? agentArgs : "";
        // Write a debug trace to %TEMP% first — guaranteed writable, visible even on crashes
        String tmpErr = System.getProperty("java.io.tmpdir") + "/dps_agent_debug.txt";
        try (java.io.PrintWriter dbg = new java.io.PrintWriter(new FileWriter(tmpErr, false))) {
            dbg.println("agentmain invoked. logPath=" + logPath);
            dbg.println("java.version=" + System.getProperty("java.version"));
        } catch (Exception ignored) {}

        System.out.println("[DPS] Agent loaded (v28). Log: " + logPath);

        // Write any exception to an error file alongside the log so we can always diagnose
        String errPath = logPath + ".err";
        try {
            _agentmainBody(logPath, errPath);
        } catch (Throwable t) {
            // Write to both the err file and the tmp debug file
            try (java.io.PrintWriter pw = new java.io.PrintWriter(new FileWriter(errPath, false))) {
                pw.println("agentmain threw: " + t);
                t.printStackTrace(pw);
            } catch (Exception ignored) {}
            try (java.io.PrintWriter dbg = new java.io.PrintWriter(new FileWriter(tmpErr, true))) {
                dbg.println("THREW: " + t);
                t.printStackTrace(dbg);
            } catch (Exception ignored) {}
            throw new RuntimeException("DPS agent init failed: " + t, t);
        }
    }

    private static void _agentmainBody(String logPath, String errPath) throws Exception {
        removeAll();
        if (watchThread != null) { watchThread.interrupt(); watchThread = null; }
        if (logWriter != null) { try { logWriter.close(); } catch (IOException ignored) {} }

        try {
            logWriter = new BufferedWriter(new FileWriter(logPath, false));
            writeEvent("AGENT_READY", "v28");
        } catch (IOException e) {
            System.err.println("[DPS] Cannot open log: " + e.getMessage());
            // Write error details so they're visible
            try (java.io.PrintWriter pw = new java.io.PrintWriter(new FileWriter(errPath, false))) {
                pw.println("Cannot open log file: " + logPath);
                pw.println(e.toString());
            } catch (Exception ignored) {}
            return;
        }

        String lockPath = logPath + ".lock";
        try { new File(lockPath).createNewFile(); } catch (IOException ignored) {}

        Thread t = new Thread(() -> {
            for (int i = 0; i < 30; i++) {
                try {
                    GameWindow gw = Client.Companion.getGameWindow();
                    if (gw != null) {
                        attachDocument(gw);
                        attachHpTracker(gw);
                        startWatchThread(lockPath);
                        return;
                    }
                } catch (Exception e) { /* not ready */ }
                try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
            }
            writeEvent("ERROR", "GameWindow not found");
        }, "DPS-Attach");
        t.setDaemon(true);
        t.start();
    }

    /**
     * Get the Wyvern style name for text at the given offset.
     * The game stores style as a custom attribute "wyvern-style-name":
     *   "hit"    = outgoing damage (blue)
     *   "damage" = incoming damage (red)
     *   "none"   = normal text
     *   "system" = system messages
     */
    private static String getStyleName(StyledDocument doc, int offset) {
        try {
            Element el = doc.getCharacterElement(offset);
            AttributeSet attrs = el.getAttributes();
            Object val = attrs.getAttribute("wyvern-style-name");
            if (val != null) return val.toString();
        } catch (Exception ignored) {}
        return "";
    }

    private static void attachDocument(GameWindow gw) {
        ServerOutput so = gw.getServerOutput();
        if (so == null) { writeEvent("ERROR", "ServerOutput null"); return; }

        Document rawDoc = so.getDocument();
        if (!(rawDoc instanceof StyledDocument)) {
            writeEvent("ERROR", "Document is not StyledDocument");
            return;
        }
        StyledDocument doc = (StyledDocument) rawDoc;

        DocumentListener listener = new DocumentListener() {
            @Override
            public void insertUpdate(DocumentEvent e) {
                if (!active) return;
                try {
                    int offset = e.getOffset();
                    int length = e.getLength();
                    String text = doc.getText(offset, length).trim();
                    if (text.isEmpty()) return;

                    // Get the exact style name: "damage" = incoming, "hit" = outgoing
                    String styleName = getStyleName(doc, offset);

                    String[] lines = text.split("\n");
                    for (String line : lines) {
                        line = line.trim();
                        if (line.isEmpty()) continue;
                        processLine(line, styleName);
                    }
                } catch (BadLocationException ignored) {}
            }
            @Override public void removeUpdate(DocumentEvent e) {}
            @Override public void changedUpdate(DocumentEvent e) {}
        };

        doc.addDocumentListener(listener);
        activeDocument = doc;
        activeListener = listener;
        active = true;
        System.out.println("[DPS] Document listener attached (styled)");
        writeEvent("ATTACHED", "v28");
    }

    /**
     * Attach HP tracking via reflection.
     * 4.2.2 (standalone): getStatModel() → StatModel with observer/callback pattern
     * 4.1.3 (Steam):      getStatDisplay() → StatView with getHp() returning Pair<Int,Int>
     *                     We poll getHp() every 200ms instead of using an observer.
     */
    private static void attachHpTracker(GameWindow gw) {
        try {
            // --- Try 4.2.2 observer pattern first ---
            Method getStatModelMethod = null;
            try {
                getStatModelMethod = gw.getClass().getMethod("getStatModel");
            } catch (NoSuchMethodException ignored) {}

            if (getStatModelMethod != null) {
                Object sm = getStatModelMethod.invoke(gw);
                if (sm == null) {
                    System.out.println("[DPS] StatModel is null");
                    return;
                }

                Class<?> observerClass = null;
                try {
                    observerClass = Class.forName("wyvern.client.StatModel$Observer");
                } catch (ClassNotFoundException e) {
                    System.err.println("[DPS] StatModel$Observer not found");
                    return;
                }

                InvocationHandler handler = new InvocationHandler() {
                    @Override
                    public Object invoke(Object proxy, Method method, Object[] args) {
                        if ("onHpChanged".equals(method.getName()) && args != null && args.length >= 2) {
                            int hp = (Integer) args[0];
                            int prev = lastHp;
                            lastHp = hp;
                            if (prev > 0 && hp < prev) {
                                int dmg = prev - hp;
                                String msg = pendingIncomingMsg;
                                long msgTs = pendingIncomingTs;
                                long now = System.currentTimeMillis();
                                if (msg != null && (now - msgTs) < 500) {
                                    pendingIncomingMsg = null;
                                    writeEvent("IN", dmg + "|" + msg);
                                } else {
                                    writeEvent("IN", String.valueOf(dmg));
                                }
                            }
                        }
                        return null;
                    }
                };

                statProxy = Proxy.newProxyInstance(
                    observerClass.getClassLoader(),
                    new Class<?>[] { observerClass },
                    handler
                );

                Method addObs = sm.getClass().getMethod("addObserver", observerClass);
                addObs.invoke(sm, statProxy);
                statModel = sm;

                try {
                    Method syncAll = sm.getClass().getMethod("syncAll", observerClass);
                    syncAll.invoke(sm, statProxy);
                } catch (Exception e) {
                    System.out.println("[DPS] syncAll not available: " + e.getMessage());
                }

                System.out.println("[DPS] HP tracker attached via StatModel observer (4.2.2)");
                return;
            }

            // --- Fall back to 4.1.3 polling via getStatDisplay().getHp() ---
            Method getStatDisplayMethod = null;
            try {
                getStatDisplayMethod = gw.getClass().getMethod("getStatDisplay");
            } catch (NoSuchMethodException ignored) {}

            if (getStatDisplayMethod == null) {
                System.out.println("[DPS] No HP tracking API found");
                return;
            }

            final Object statView = getStatDisplayMethod.invoke(gw);
            if (statView == null) {
                System.out.println("[DPS] StatView is null");
                return;
            }

            final Method getHpMethod = statView.getClass().getMethod("getHp");

            // Poll HP every 200ms in a daemon thread
            Thread hpPoll = new Thread(() -> {
                while (!Thread.currentThread().isInterrupted() && active) {
                    try {
                        Object hpPair = getHpMethod.invoke(statView);
                        if (hpPair != null) {
                            // getHp() returns Pair<Integer, Integer> — first = current, second = max
                            // Pair has .getFirst() / .component1() in Kotlin
                            int hp = -1;
                            try {
                                Method first = hpPair.getClass().getMethod("getFirst");
                                hp = (Integer) first.invoke(hpPair);
                            } catch (NoSuchMethodException e) {
                                // Try Kotlin component1()
                                Method comp1 = hpPair.getClass().getMethod("component1");
                                hp = (Integer) comp1.invoke(hpPair);
                            }
                            int prev = lastHp;
                            lastHp = hp;
                            if (prev > 0 && hp < prev) {
                                int dmg = prev - hp;
                                String msg = pendingIncomingMsg;
                                long msgTs = pendingIncomingTs;
                                long now = System.currentTimeMillis();
                                if (msg != null && (now - msgTs) < 500) {
                                    pendingIncomingMsg = null;
                                    writeEvent("IN", dmg + "|" + msg);
                                } else {
                                    writeEvent("IN", String.valueOf(dmg));
                                }
                            }
                        }
                    } catch (Exception e) {
                        // Stop polling if API breaks
                        System.err.println("[DPS] HP poll error: " + e);
                        return;
                    }
                    try { Thread.sleep(200); } catch (InterruptedException e) { return; }
                }
            }, "DPS-HpPoll");
            hpPoll.setDaemon(true);
            hpPoll.start();

            System.out.println("[DPS] HP tracker attached via StatView polling (4.1.3)");

        } catch (Throwable t) {
            System.err.println("[DPS] HP tracker failed (non-fatal): " + t);
            t.printStackTrace();
        }
    }

    private static void processLine(String line, String styleName) {
        if (line.contains("You have died") || line.contains("You die")) {
            writeEvent("DEATH", line);
            return;
        }

        if (KILL_PATTERN.matcher(line).find()) {
            writeEvent("KILL", line);
        }

        // Direction detection:
        // 4.2.2 standalone: wyvern-style-name attribute is set ("hit" = out, "damage" = in)
        // 4.1.3 Steam:      attribute is absent — use text heuristic instead
        boolean isOutgoing;
        boolean isIncoming;
        if (!styleName.isEmpty()) {
            isOutgoing = "hit".equals(styleName);
            isIncoming = "damage".equals(styleName);
        } else {
            // Lines from the player start with "You " or "Your "
            isOutgoing = line.startsWith("You ") || line.startsWith("Your ");
            // Incoming = not from player and has a damage number
            isIncoming = !isOutgoing && DAMAGE_PATTERN.matcher(line).find();
        }

        if (isIncoming) {
            pendingIncomingMsg = line;
            pendingIncomingTs = System.currentTimeMillis();
            return;
        }

        if (isOutgoing) {
            Matcher dmg = DAMAGE_PATTERN.matcher(line);
            if (!dmg.find()) return;
            String damage = dmg.group(1);
            writeEvent("OUT", damage + "|" + line);
        }
    }

    private static void removeAll() {
        active = false;
        if (activeDocument != null && activeListener != null) {
            try { activeDocument.removeDocumentListener(activeListener); } catch (Exception ignored) {}
        }
        activeDocument = null;
        activeListener = null;

        if (statModel != null && statProxy != null) {
            try {
                Class<?> obsClass = Class.forName("wyvern.client.StatModel$Observer");
                Method removeObs = statModel.getClass().getMethod("removeObserver", obsClass);
                removeObs.invoke(statModel, statProxy);
                System.out.println("[DPS] HP tracker removed");
            } catch (Throwable ignored) {}
        }
        statModel = null;
        statProxy = null;
        lastHp = -1;
        pendingIncomingMsg = null;
    }

    private static void startWatchThread(String lockPath) {
        watchThread = new Thread(() -> {
            File lockFile = new File(lockPath);
            while (!Thread.currentThread().isInterrupted()) {
                try { Thread.sleep(500); } catch (InterruptedException e) { return; }
                if (!lockFile.exists()) {
                    removeAll();
                    if (logWriter != null) { try { logWriter.close(); } catch (IOException ignored) {} logWriter = null; }
                    return;
                }
            }
        }, "DPS-Watch");
        watchThread.setDaemon(true);
        watchThread.start();
    }

    private static synchronized void writeEvent(String type, String data) {
        if (logWriter == null) return;
        try {
            logWriter.write(type + "|" + System.currentTimeMillis() + "|" + data);
            logWriter.newLine();
            logWriter.flush();
        } catch (IOException e) {
            System.err.println("[DPS] Write error: " + e.getMessage());
        }
    }
}

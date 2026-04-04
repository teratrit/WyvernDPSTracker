package dps21;

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
        String logPath = agentArgs;
        System.out.println("[DPS] Agent loaded (v20). Log: " + logPath);

        removeAll();
        if (watchThread != null) { watchThread.interrupt(); watchThread = null; }
        if (logWriter != null) { try { logWriter.close(); } catch (IOException ignored) {} }

        try {
            logWriter = new BufferedWriter(new FileWriter(logPath, false));
            writeEvent("AGENT_READY", "v20");
        } catch (IOException e) {
            System.err.println("[DPS] Cannot open log: " + e.getMessage());
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
     * Get the style name for text at the given offset.
     * Game uses named styles: "damage" (incoming/red), "hit" (outgoing/blue), etc.
     */
    private static int debugCount = 0;

    private static String getStyleName(StyledDocument doc, int offset) {
        try {
            Element el = doc.getCharacterElement(offset);
            AttributeSet attrs = el.getAttributes();

            // Debug: dump first 15 style lookups
            if (debugCount < 15) {
                debugCount++;
                StringBuilder sb = new StringBuilder();
                sb.append("attrs=[");
                java.util.Enumeration<?> names = attrs.getAttributeNames();
                while (names.hasMoreElements()) {
                    Object key = names.nextElement();
                    Object val = attrs.getAttribute(key);
                    sb.append(key).append("=").append(val);
                    if (val != null) sb.append("(").append(val.getClass().getSimpleName()).append(")");
                    sb.append(", ");
                }
                sb.append("]");
                AttributeSet parent = attrs.getResolveParent();
                if (parent != null) {
                    sb.append(" parent=").append(parent.getClass().getSimpleName());
                    if (parent instanceof Style) {
                        sb.append(" name=").append(((Style) parent).getName());
                    }
                }
                writeEvent("DBG_STYLE", sb.toString());
            }

            AttributeSet parent = attrs.getResolveParent();
            if (parent instanceof Style) {
                String name = ((Style) parent).getName();
                if (name != null) return name;
            }
            Object nameAttr = attrs.getAttribute(StyleConstants.NameAttribute);
            if (nameAttr != null) return nameAttr.toString();
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
        writeEvent("ATTACHED", "v20");
    }

    /**
     * Attach to StatModel for HP tracking using reflection + dynamic proxy.
     */
    private static void attachHpTracker(GameWindow gw) {
        try {
            Object sm = gw.getStatModel();
            if (sm == null) {
                System.out.println("[DPS] StatModel is null, no HP tracking");
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
                            // Grab pending incoming message if recent (within 500ms)
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

            System.out.println("[DPS] HP tracker attached via dynamic proxy");
        } catch (Throwable t) {
            System.err.println("[DPS] HP tracker failed (non-fatal): " + t);
            t.printStackTrace();
        }
    }

    private static void processLine(String line, String styleName) {
        // Player death
        if (line.contains("You have died") || line.contains("You die")) {
            writeEvent("DEATH", line);
            return;
        }

        if (KILL_PATTERN.matcher(line).find()) {
            writeEvent("KILL", line);
        }

        // "damage" style = incoming (red text) — store as pending for HP tracker
        if ("damage".equals(styleName)) {
            pendingIncomingMsg = line;
            pendingIncomingTs = System.currentTimeMillis();
            return;
        }

        // "hit" style = outgoing (blue text)
        if ("hit".equals(styleName)) {
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

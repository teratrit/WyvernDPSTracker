package dps9;

import wyvern.client.Client;
import wyvern.client.GameWindow;
import wyvern.client.ServerOutput;

import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import javax.swing.text.BadLocationException;
import javax.swing.text.Document;
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
    private static final Pattern OUTGOING_RE =
        Pattern.compile("^You\\s+\\w+", Pattern.CASE_INSENSITIVE);
    private static final Pattern KILL_PATTERN =
        Pattern.compile("(?:You killed |You destroy |is destroyed|is dead)", Pattern.CASE_INSENSITIVE);

    private static BufferedWriter logWriter;
    private static Document activeDocument;
    private static DocumentListener activeListener;
    private static Object statProxy;       // dynamic proxy for StatModel.Observer
    private static Object statModel;       // the StatModel instance
    private static volatile boolean active = false;
    private static Thread watchThread;

    // HP tracking
    private static volatile int lastHp = -1;

    public static void agentmain(String agentArgs, Instrumentation inst) {
        String logPath = agentArgs;
        System.out.println("[DPS] Agent loaded. Log: " + logPath);

        removeAll();
        if (watchThread != null) { watchThread.interrupt(); watchThread = null; }
        if (logWriter != null) { try { logWriter.close(); } catch (IOException ignored) {} }

        try {
            logWriter = new BufferedWriter(new FileWriter(logPath, false));
            writeEvent("AGENT_READY", "v9");
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

    private static void attachDocument(GameWindow gw) {
        ServerOutput so = gw.getServerOutput();
        if (so == null) { writeEvent("ERROR", "ServerOutput null"); return; }

        Document doc = so.getDocument();
        DocumentListener listener = new DocumentListener() {
            @Override
            public void insertUpdate(DocumentEvent e) {
                if (!active) return;
                try {
                    String text = e.getDocument().getText(e.getOffset(), e.getLength()).trim();
                    if (text.isEmpty()) return;
                    for (String line : text.split("\n")) {
                        line = line.trim();
                        if (line.isEmpty()) continue;
                        processLine(line);
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
        System.out.println("[DPS] Document listener attached");
        writeEvent("ATTACHED", "v9");
    }

    /**
     * Attach to StatModel for HP tracking using reflection + dynamic proxy.
     * This avoids direct references to StatModel.Observer which may differ
     * between game client versions.
     */
    private static void attachHpTracker(GameWindow gw) {
        try {
            Object sm = gw.getStatModel();
            if (sm == null) {
                System.out.println("[DPS] StatModel is null, no HP tracking");
                return;
            }

            // Find the Observer interface dynamically
            Class<?> observerClass = null;
            for (Class<?> inner : sm.getClass().getInterfaces()) {
                // skip
            }
            // It's an inner interface of StatModel
            try {
                observerClass = Class.forName("wyvern.client.StatModel$Observer");
            } catch (ClassNotFoundException e) {
                System.err.println("[DPS] StatModel$Observer not found");
                return;
            }

            // Create a dynamic proxy that handles onHpChanged
            InvocationHandler handler = new InvocationHandler() {
                @Override
                public Object invoke(Object proxy, Method method, Object[] args) {
                    if ("onHpChanged".equals(method.getName()) && args != null && args.length >= 2) {
                        int hp = (Integer) args[0];
                        int prev = lastHp;
                        lastHp = hp;
                        if (prev > 0 && hp < prev) {
                            int dmg = prev - hp;
                            writeEvent("IN", String.valueOf(dmg));
                        }
                    }
                    // Return null for all void methods
                    return null;
                }
            };

            statProxy = Proxy.newProxyInstance(
                observerClass.getClassLoader(),
                new Class<?>[] { observerClass },
                handler
            );

            // Call sm.addObserver(proxy) via reflection
            Method addObs = sm.getClass().getMethod("addObserver", observerClass);
            addObs.invoke(sm, statProxy);
            statModel = sm;

            // Seed initial HP via syncAll
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

    private static void processLine(String line) {
        if (KILL_PATTERN.matcher(line).find()) {
            writeEvent("KILL", line);
        }

        Matcher dmg = DAMAGE_PATTERN.matcher(line);
        if (!dmg.find()) return;
        String damage = dmg.group(1);

        // Outgoing only — incoming is handled by HP tracking
        if (OUTGOING_RE.matcher(line).find()) {
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

        // Remove stat observer via reflection
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

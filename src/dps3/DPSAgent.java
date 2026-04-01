package dps3;

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
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Java agent — hooks into the Wyvern client's ServerOutput to capture
 * Training Dummy damage events with millisecond timestamps and full message text.
 *
 * Lifecycle:
 * - On attach: adds a DocumentListener to ServerOutput
 * - Watches for a signal file ({logPath}.lock) — when it disappears, removes the listener
 * - On re-attach: removes old listener first, then adds a fresh one
 *
 * Log format: HIT|timestamp_ms|damage|full_message_line
 */
public class DPSAgent {

    private static final Pattern DAMAGE_PATTERN =
        Pattern.compile("for\\s+(\\d+)\\s+damage", Pattern.CASE_INSENSITIVE);
    private static final Pattern TARGET_PATTERN =
        Pattern.compile("Training\\s+Dummy", Pattern.CASE_INSENSITIVE);
    private static final Pattern KILL_PATTERN =
        Pattern.compile("Training\\s+Dummy.*(?:killed|destroyed|is dead|dies)", Pattern.CASE_INSENSITIVE);

    private static BufferedWriter logWriter;
    private static Document activeDocument;
    private static DocumentListener activeListener;
    private static volatile boolean active = false;
    private static Thread watchThread;

    public static void agentmain(String agentArgs, Instrumentation inst) {
        String logPath = agentArgs;
        System.out.println("[DPS] Agent loaded. Log: " + logPath);

        // Clean up previous listener
        removeListener();

        // Stop previous watch thread
        if (watchThread != null) {
            watchThread.interrupt();
            watchThread = null;
        }

        // Close previous writer
        if (logWriter != null) {
            try { logWriter.close(); } catch (IOException ignored) {}
        }

        try {
            logWriter = new BufferedWriter(new FileWriter(logPath, false));
            writeEvent("AGENT_READY", "v2");
        } catch (IOException e) {
            System.err.println("[DPS] Cannot open log: " + e.getMessage());
            return;
        }

        // Create signal file — GUI deletes this on close to trigger listener removal
        String lockPath = logPath + ".lock";
        try {
            new File(lockPath).createNewFile();
        } catch (IOException ignored) {}

        // Attach listener
        Thread t = new Thread(() -> {
            for (int i = 0; i < 30; i++) {
                try {
                    GameWindow gw = Client.Companion.getGameWindow();
                    if (gw != null) {
                        attachListener(gw);
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

    private static void attachListener(GameWindow gw) {
        ServerOutput so = gw.getServerOutput();
        if (so == null) {
            writeEvent("ERROR", "ServerOutput null");
            return;
        }

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

                        // Debug: log all lines containing "damage" or "dummy"
                        String lower = line.toLowerCase();
                        if (lower.contains("damage") || lower.contains("dummy")) {
                            writeEvent("DBG", line);
                        }

                        if (!TARGET_PATTERN.matcher(line).find()) continue;

                        if (KILL_PATTERN.matcher(line).find()) {
                            writeEvent("KILL", line);
                        } else {
                            Matcher m = DAMAGE_PATTERN.matcher(line);
                            if (m.find()) {
                                writeEvent("HIT", m.group(1) + "|" + line);
                            }
                        }
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

        System.out.println("[DPS] Listener attached to ServerOutput");
        writeEvent("ATTACHED", "v2");
    }

    private static void removeListener() {
        active = false;
        if (activeDocument != null && activeListener != null) {
            try {
                activeDocument.removeDocumentListener(activeListener);
                System.out.println("[DPS] Listener removed from ServerOutput");
            } catch (Exception e) {
                System.err.println("[DPS] Error removing listener: " + e.getMessage());
            }
        }
        activeDocument = null;
        activeListener = null;
    }

    /**
     * Watch the lock file. When it disappears (GUI closed), remove the listener
     * and close the log writer.
     */
    private static void startWatchThread(String lockPath) {
        watchThread = new Thread(() -> {
            File lockFile = new File(lockPath);
            while (!Thread.currentThread().isInterrupted()) {
                try { Thread.sleep(500); } catch (InterruptedException e) { return; }
                if (!lockFile.exists()) {
                    System.out.println("[DPS] Lock file gone — removing listener");
                    removeListener();
                    if (logWriter != null) {
                        try { logWriter.close(); } catch (IOException ignored) {}
                        logWriter = null;
                    }
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

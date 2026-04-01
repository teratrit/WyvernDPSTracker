package dps4;

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
 * ALL combat damage (outgoing and incoming) with millisecond timestamps.
 *
 * Event format:
 *   OUT|timestamp_ms|damage|full_message   (you dealt damage)
 *   IN|timestamp_ms|damage|full_message    (you took damage)
 *   KILL|timestamp_ms|full_message         (monster killed)
 */
public class DPSAgent {

    private static final Pattern DAMAGE_PATTERN =
        Pattern.compile("for\\s+(\\d+)\\s+damage", Pattern.CASE_INSENSITIVE);

    // Outgoing: lines starting with "You " that contain damage
    private static final Pattern OUTGOING_RE =
        Pattern.compile("^You\\s+\\w+", Pattern.CASE_INSENSITIVE);

    // Incoming: "[Monster] [verb]s you" patterns
    private static final Pattern INCOMING_RE =
        Pattern.compile("(?:hits|damages|slashes|stabs|bites|claws|burns|zaps|smashes|crushes|strikes|blasts|freezes|shocks|drowns|staggers|cuts|pierces|impales)\\s+you", Pattern.CASE_INSENSITIVE);

    // Kill detection
    private static final Pattern KILL_PATTERN =
        Pattern.compile("(?:You killed |You destroy |is destroyed|is dead)", Pattern.CASE_INSENSITIVE);

    private static BufferedWriter logWriter;
    private static Document activeDocument;
    private static DocumentListener activeListener;
    private static volatile boolean active = false;
    private static Thread watchThread;

    public static void agentmain(String agentArgs, Instrumentation inst) {
        String logPath = agentArgs;
        System.out.println("[DPS4] Agent loaded. Log: " + logPath);

        removeListener();

        if (watchThread != null) {
            watchThread.interrupt();
            watchThread = null;
        }

        if (logWriter != null) {
            try { logWriter.close(); } catch (IOException ignored) {}
        }

        try {
            logWriter = new BufferedWriter(new FileWriter(logPath, false));
            writeEvent("AGENT_READY", "v4");
        } catch (IOException e) {
            System.err.println("[DPS4] Cannot open log: " + e.getMessage());
            return;
        }

        String lockPath = logPath + ".lock";
        try { new File(lockPath).createNewFile(); } catch (IOException ignored) {}

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
        }, "DPS4-Attach");
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

        System.out.println("[DPS4] Listener attached to ServerOutput");
        writeEvent("ATTACHED", "v4");
    }

    private static void processLine(String line) {
        // Check for kill
        if (KILL_PATTERN.matcher(line).find()) {
            writeEvent("KILL", line);
            // Don't return — a kill line might also contain damage
        }

        Matcher dmg = DAMAGE_PATTERN.matcher(line);
        if (!dmg.find()) return;
        String damage = dmg.group(1);

        // Outgoing: "You [verb] ..."
        if (OUTGOING_RE.matcher(line).find()) {
            writeEvent("OUT", damage + "|" + line);
            return;
        }

        // Incoming: "[Monster] [verb]s you"
        if (INCOMING_RE.matcher(line).find()) {
            writeEvent("IN", damage + "|" + line);
            return;
        }
    }

    private static void removeListener() {
        active = false;
        if (activeDocument != null && activeListener != null) {
            try {
                activeDocument.removeDocumentListener(activeListener);
                System.out.println("[DPS4] Listener removed");
            } catch (Exception e) {
                System.err.println("[DPS4] Error removing listener: " + e.getMessage());
            }
        }
        activeDocument = null;
        activeListener = null;
    }

    private static void startWatchThread(String lockPath) {
        watchThread = new Thread(() -> {
            File lockFile = new File(lockPath);
            while (!Thread.currentThread().isInterrupted()) {
                try { Thread.sleep(500); } catch (InterruptedException e) { return; }
                if (!lockFile.exists()) {
                    System.out.println("[DPS4] Lock gone — removing listener");
                    removeListener();
                    if (logWriter != null) {
                        try { logWriter.close(); } catch (IOException ignored) {}
                        logWriter = null;
                    }
                    return;
                }
            }
        }, "DPS4-Watch");
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
            System.err.println("[DPS4] Write error: " + e.getMessage());
        }
    }
}

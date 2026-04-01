package dps;

import com.sun.tools.attach.VirtualMachine;
import com.sun.tools.attach.VirtualMachineDescriptor;

import java.io.File;
import java.util.List;

/**
 * Finds the running Wyvern JVM and loads the DPS agent into it.
 *
 * Usage: java dps.DPSAttacher <agent.jar path> <log file path> [pid]
 *
 * If no PID is provided, scans for a JVM whose display name contains "wyvern".
 * If VirtualMachine.list() can't find it (e.g. JPackage app), tries the PID
 * from a Windows process lookup.
 */
public class DPSAttacher {

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: java dps.DPSAttacher <agent.jar> <log-file> [pid]");
            System.exit(1);
        }

        String agentJar = new File(args[0]).getAbsolutePath();
        String logFile = new File(args[1]).getAbsolutePath();
        String pid = args.length >= 3 ? args[2] : null;

        if (!new File(agentJar).exists()) {
            System.err.println("Agent JAR not found: " + agentJar);
            System.exit(1);
        }

        // Find Wyvern PID if not provided
        if (pid == null) {
            pid = findWyvernPid();
        }

        if (pid == null) {
            System.err.println("Could not find Wyvern JVM. Is the game running?");
            System.err.println("You can provide the PID manually as the third argument.");
            System.exit(1);
        }

        System.out.println("Attaching to Wyvern JVM (PID " + pid + ")...");
        System.out.println("Agent JAR: " + agentJar);
        System.out.println("Log file: " + logFile);

        try {
            VirtualMachine vm = VirtualMachine.attach(pid);
            vm.loadAgent(agentJar, logFile);
            vm.detach();
            System.out.println("Agent loaded successfully!");
        } catch (Exception e) {
            System.err.println("Failed to attach: " + e.getMessage());
            e.printStackTrace();
            System.exit(1);
        }
    }

    /**
     * Try to find the Wyvern JVM PID.
     * Strategy 1: VirtualMachine.list() — works for standard java launches
     * Strategy 2: Process name lookup — works for JPackage/native launchers
     */
    private static String findWyvernPid() {
        String myPid = String.valueOf(ProcessHandle.current().pid());

        // Strategy 1: JVM enumeration — look for wyvern.client.Client specifically
        try {
            List<VirtualMachineDescriptor> vms = VirtualMachine.list();
            for (VirtualMachineDescriptor vmd : vms) {
                // Skip our own process
                if (vmd.id().equals(myPid)) continue;
                String name = vmd.displayName().toLowerCase();
                // Match the game client class, not just any "wyvern" in path
                if (name.contains("wyvern.client") || name.contains("wyvernwindowsclient")) {
                    System.out.println("Found via JVM list: " + vmd.displayName()
                            + " (PID " + vmd.id() + ")");
                    return vmd.id();
                }
            }
        } catch (Exception e) {
            System.out.println("VirtualMachine.list() failed: " + e.getMessage());
        }

        // Strategy 2: Windows process lookup
        try {
            ProcessHandle.allProcesses()
                .filter(p -> p.info().command().isPresent())
                .forEach(p -> {
                    String cmd = p.info().command().get().toLowerCase();
                    if (cmd.contains("wyvern")) {
                        System.out.println("Found process: PID=" + p.pid()
                                + " cmd=" + p.info().command().get());
                    }
                });

            // Look for the specific exe
            return ProcessHandle.allProcesses()
                .filter(p -> p.info().command().orElse("").toLowerCase().contains("wyvernwindowsclient"))
                .map(p -> String.valueOf(p.pid()))
                .findFirst()
                .orElse(null);
        } catch (Exception e) {
            System.out.println("Process scan failed: " + e.getMessage());
        }

        return null;
    }
}

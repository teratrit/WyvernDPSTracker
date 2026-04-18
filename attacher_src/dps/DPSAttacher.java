package dps;

import com.sun.tools.attach.VirtualMachine;
import com.sun.tools.attach.VirtualMachineDescriptor;

import java.util.List;

public class DPSAttacher {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: DPSAttacher <agent.jar> <log.path>");
            System.exit(1);
        }
        String agentJar = args[0];
        String logPath  = args[1];

        List<VirtualMachineDescriptor> vms = VirtualMachine.list();
        VirtualMachineDescriptor target = null;
        List<VirtualMachineDescriptor> candidates = new java.util.ArrayList<>();

        for (VirtualMachineDescriptor vmd : vms) {
            String disp    = vmd.displayName();
            String dispLow = disp.toLowerCase();
            // Skip our own tools
            if (disp.contains("DPSAttacher") || disp.contains("DPSTracker")) continue;
            // Match: class name (wyvern.client.Client), jar name (wyvern-client-*.jar), or path contains "wyvern"
            if (dispLow.contains("wyvern")) {
                candidates.add(vmd);
            }
        }

        if (candidates.size() == 1) {
            target = candidates.get(0);
        } else if (candidates.size() > 1) {
            // Prefer the one whose display name looks like a main class (contains a dot)
            for (VirtualMachineDescriptor vmd : candidates) {
                if (vmd.displayName().contains(".")) { target = vmd; break; }
            }
            if (target == null) target = candidates.get(0);
        }

        if (target == null) {
            System.err.println("No Wyvern JVM found. Running JVMs:");
            for (VirtualMachineDescriptor vmd : vms) {
                System.err.println("  " + vmd.id() + " : " + vmd.displayName());
            }
            System.exit(2);
        }

        System.out.println("Attaching to PID " + target.id() + ": " + target.displayName());
        VirtualMachine vm = VirtualMachine.attach(target);
        try {
            vm.loadAgent(agentJar, logPath);
            System.out.println("Agent loaded.");
        } finally {
            vm.detach();
        }
    }
}

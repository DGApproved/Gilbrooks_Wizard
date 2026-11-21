package usbprep.gui;

import java.awt.BorderLayout;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridLayout;
import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.RandomAccessFile;
import java.lang.management.ManagementFactory;
import java.nio.charset.Charset;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JCheckBoxMenuItem;
import javax.swing.JComboBox;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JMenu;
import javax.swing.JMenuBar;
import javax.swing.JMenuItem;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTextArea;
import javax.swing.JTextField;
import javax.swing.SwingUtilities;
import javax.swing.Timer;

/**
 * Java 8 Swing front end. It performs NO storage operations itself: every action runs the Python
 * backend (`python3 -m usbprep ...`), which owns detection, safety checks, TOCTOU revalidation,
 * privilege elevation (pkexec/sudo) and the real partition/mount/fsck tools. Dry run is the default.
 */
public final class UsbPrepGui {
    static final String EXTRAS = "Extras";
    static final String M_CHECK = "Check drive for errors";
    static final String M_REPAIR = "Auto repair detected errors";
    static final String M_REFORMAT = "Reformat...";
    static final String M_VMDK = "Prepare VMDK file for VirtualBox...";
    static final String M_VM_OUT = "Hand drive to VM (VirtualBox)...";
    static final String M_VM_BACK = "Return drive from VM...";
    static final String M_SCAN_BEFORE_MOUNT = "Read-only bad-block scan before mount";
    static final String M_SCAN_NOW = "Run bad-block scan now...";
    static final String M_MATRIX = "Support matrix";
    static final String CLONE = "Clone";
    static final String M_CLONE = "Clone ext4 (A.I.) partition from one USB to another...";
    static final String SCRIPTS = "Scripts";
    static final String DATA = "Data";
    static final String M_DATA_BACKUP = "Backup current user to /Data...";
    static final String M_DATA_RESTORE_SHARED = "Restore shared data (Music/Pictures/Videos/Documents)...";
    static final String M_DATA_RESTORE_OS = "Restore OS-specific data...";
    static final String M_DATA_LIST = "List backups";
    static final String DATA_WARNING = "Backup copies ALL files in your home folder (including hidden files) to /Data on the\n"
            + "USB ext4 partition. Portable files (Music/Pictures/Videos/Documents by extension) go to /Data/Shared,\n"
            + "everything else to /Data/<OS>/<machine>/<user>.\n\n"
            + "* As requested, the COPIES get permission 777: any local user on any computer where this USB is\n"
            + "  mounted can read and change them. Your originals are not changed.\n"
            + "* Your home may contain passwords, tokens and private keys. No confidentiality is promised.\n"
            + "* A live account is not an application-consistent snapshot; unreadable/locked files are skipped and listed.\n"
            + "* Original permissions are recorded so Restore can put them back.\n\nBack up now?";
    static final String M_SCRIPTS_REFRESH = "Refresh script list";
    static final String FULL_FORMAT = "Full format (slower; writes the entire partition)";
    static final String FULL_FORMAT_WARNING = "Full format zero-fills EVERY byte of each partition on the selected USB\n"
            + "(FAT32, ext4 and swap, strictly inside their boundaries), then creates the filesystems.\n\n"
            + "* It takes much longer and writes the whole partition, which adds flash wear and may reduce lifespan.\n"
            + "* Free space is not 'formatted as a type': it is zeroed, then FAT32/ext4 metadata is written.\n"
            + "* Any later speed benefit depends on the device and workload and is NOT guaranteed.\n"
            + "* It is NOT a secure erase, does NOT verify the media and does NOT run the bad-block scan.\n"
            + "* The partition table (MBR/GPT) is not overwritten; boot code is installed after formatting.\n\n"
            + "Use full format?";
    static final String STAR_ON = "\u2605", STAR_OFF = "\u2606";
    static final String STAR_WARNING = "Mark this drive as a DEDICATED USB drive?\n\n"
            + "- Detection did not classify it as a USB flash drive (it may be a USB hard disk/SSD, card reader, ...).\n"
            + "- Initialize / formatting it will ERASE EVERYTHING on it.\n"
            + "- This project was designed for USB flash drives; using other drives is your responsibility.\n"
            + "- The star never overrides the system-disk / internal-disk protections, and every other\n"
            + "  safeguard (dry run, typed confirmation, re-check before each action) still applies.\n\n"
            + "Nothing is run by this dialog; it only saves the mark.";
    static final String JAVA_DOWNLOAD = "https://adoptium.net/temurin/releases/";

    /** One detected drive as reported by the backend. */
    static final class Drive {
        final String path, label, status, token, serial, size, starred, starRefusal, sessionKey, layout;
        final boolean source, starAllowed, detected, serialPresent, prepared;
        Drive(Map<String, Object> d, boolean source) {
            path = str(d.get("path"));
            status = str(d.get("status"));
            starred = str(d.get("starred"));
            starAllowed = Boolean.TRUE.equals(d.get("star_allowed"));
            starRefusal = str(d.get("star_refusal"));
            detected = !Boolean.FALSE.equals(d.get("detected_dedicated"));   // older backends: keep old presentation
            serialPresent = !Boolean.FALSE.equals(d.get("serial_present"));
            sessionKey = str(d.get("star_session_key"));
            layout = str(d.get("layout"));
            prepared = "prepared".equals(status) || "prepared".equals(layout);
            token = str(d.get("confirm_token"));
            serial = str(d.get("serial"));
            size = str(d.get("size_human"));
            this.source = source;
            label = path + "  " + str(d.get("model")) + "  serial=" + (serial.isEmpty() ? "?" : serial) + "  "
                    + str(d.get("size_human")) + "  [" + status + "]"
                    + ("saved".equals(starred) ? "  " + STAR_ON : "session".equals(starred) ? "  " + STAR_ON + " (this session)"
                       : detected ? "" : "  (not detected as dedicated USB)")
                    + (source ? "  (running app source)" : "");
        }
        @Override public String toString() { return label; }
    }

    final File root;
    final List<String> python;
    final JFrame frame = new JFrame("Project 1 USB Prep");
    final JComboBox<Object> selector = new JComboBox<Object>();
    final JCheckBoxMenuItem scanBeforeMount = new JCheckBoxMenuItem(M_SCAN_BEFORE_MOUNT, false);
    final JCheckBox dryRun = new JCheckBox("Dry run (show the plan only, change nothing)", true);
    final JCheckBox eraseAck = new JCheckBox("I understand ALL data on the selected drive will be erased", false);
    final JCheckBox allowAmbiguous = new JCheckBox("Override: device type is ambiguous but it IS a dedicated flash drive", false);
    final JCheckBox fullRepair = new JCheckBox("Consent: full e2fsck repair (-f -y) if the safe pass cannot fix errors", false);
    final JCheckBox fullFormat = new JCheckBox(FULL_FORMAT, false);
    final JMenu scriptsMenu = new JMenu(SCRIPTS);
    final JMenu dataMenu = new JMenu(DATA);
    String hostOs = "linux";
    final List<Drive> drives = new ArrayList<Drive>();
    final java.util.Set<String> runningScripts = java.util.Collections.synchronizedSet(new java.util.HashSet<String>());
    final JCheckBox updateApp = new JCheckBox("Replace an older app copy in EFI:/AI", false);
    final JComboBox<String> elevation = new JComboBox<String>(new String[]{"pkexec", "sudo", "none"});
    final JTextField token = new JTextField(14);
    final JLabel tokenHint = new JLabel(" ");
    final JTextArea log = new JTextArea(16, 90);
    final List<JButton> actionButtons = new ArrayList<JButton>();
    // ext4 backend checkboxes: alternative backends, mutually exclusive, all OFF by default (= native)
    final JCheckBox wslBox = new JCheckBox("WSL", false);
    final JCheckBox customWinBox = new JCheckBox("Custom driver", false);
    final JCheckBox readonlyBox = new JCheckBox("Read-only driver (ext4fuse)", false);
    final JCheckBox homebrewBox = new JCheckBox("Homebrew (anylinuxfs, read/write)", false);
    final JCheckBox customMacBox = new JCheckBox("Custom driver", false);
    final JLabel backendLabel = new JLabel("ext4 backend: native");
    final JButton mountBtn = new JButton("Mount");
    final JButton initBtn = new JButton("Initialize");
    final JButton starBtn = new JButton(STAR_OFF);
    final java.util.Set<String> sessionStars = new java.util.LinkedHashSet<String>();
    String keepSelection;
    String backendState = "READY";
    String backendChoice = "native";
    boolean busy;
    boolean syncingBoxes;
    String sourceDisk;
    boolean markConsent;

    UsbPrepGui(File root, List<String> python) {
        this.root = root;
        this.python = python;
        build();
    }

    static String str(Object o) { return o == null ? "" : String.valueOf(o); }

    // ---------------- UI ----------------
    private void build() {
        JMenuBar bar = new JMenuBar();
        JMenu extras = new JMenu(EXTRAS);
        extras.add(item(M_CHECK, new Runnable() { public void run() { checkDrive(); } }));
        extras.add(item(M_REPAIR, new Runnable() { public void run() { repairDrive(); } }));
        extras.add(item(M_REFORMAT, new Runnable() { public void run() { reformat(); } }));
        extras.add(item(M_VMDK, new Runnable() { public void run() { vmdk(); } }));
        extras.add(item(M_VM_OUT, new Runnable() { public void run() { vmHandoff(); } }));
        extras.add(item(M_VM_BACK, new Runnable() { public void run() { vmReturn(); } }));
        extras.addSeparator();
        scanBeforeMount.addActionListener(e -> {
            if (scanBeforeMount.isSelected() && !confirm("Enable the read-only bad-block scan before each mount?\n\n"
                    + "It reads the whole AI partition (slow), runs only while it is unmounted and writes nothing\n"
                    + "unless you also consent to marking unreadable blocks. Marking blocks does NOT repair failing\n"
                    + "flash memory; back up and replace a drive that reports media errors.")) {
                scanBeforeMount.setSelected(false);
            }
        });
        extras.add(scanBeforeMount);
        extras.add(item(M_SCAN_NOW, new Runnable() { public void run() { scanNow(); } }));
        extras.addSeparator();
        extras.add(item(M_MATRIX, new Runnable() { public void run() { showMatrix(); } }));
        bar.add(extras);
        JMenu clone = new JMenu(CLONE);
        clone.add(item(M_CLONE, new Runnable() { public void run() { cloneDialog(); } }));
        bar.add(clone);
        populateScripts(null);
        bar.add(scriptsMenu);
        dataMenu.add(item(M_DATA_BACKUP, new Runnable() { public void run() { dataBackup(); } }));
        dataMenu.add(item(M_DATA_RESTORE_SHARED, new Runnable() { public void run() { dataRestore(true); } }));
        dataMenu.add(item(M_DATA_RESTORE_OS, new Runnable() { public void run() { dataRestore(false); } }));
        dataMenu.add(item(M_DATA_LIST, new Runnable() { public void run() { dataList(); } }));
        bar.add(dataMenu);
        frame.setJMenuBar(bar);

        JPanel top = new JPanel(new FlowLayout(FlowLayout.LEFT));
        top.add(new JLabel("USB drive:"));
        selector.setPrototypeDisplayValue("/dev/sdX  SomeVendor Flash Drive  58.6 GiB  [eligible]  (running app source)");
        selector.addActionListener(e -> { updateTokenHint(); updateControls(); });
        top.add(selector);
        starBtn.setFont(starBtn.getFont().deriveFont(16f));
        starBtn.addActionListener(e -> toggleStar());
        top.add(starBtn);
        top.add(button("Refresh", this::refresh, false));

        JPanel opts = new JPanel(new GridLayout(0, 1));
        opts.setBorder(BorderFactory.createTitledBorder("Options"));
        opts.add(dryRun);
        opts.add(allowAmbiguous);
        opts.add(fullRepair);
        opts.add(updateApp);
        JPanel el = new JPanel(new FlowLayout(FlowLayout.LEFT, 0, 0));
        el.add(new JLabel("Elevation for root steps (native password prompt): "));
        el.add(elevation);
        opts.add(el);

        JPanel erase = new JPanel(new GridLayout(0, 1));
        erase.setBorder(BorderFactory.createTitledBorder("Destructive confirmation (prepare / duplicate / reformat)"));
        erase.add(eraseAck);
        fullFormat.setToolTipText("Unchecked = quick format (filesystem creation only). Checked = zero-fill each "
                + "partition (FAT32, ext4, swap) inside its boundaries, then create the filesystems. "
                + "Not a secure erase; no media verification.");
        fullFormat.addActionListener(e -> {
            if (fullFormat.isSelected() && !confirm(FULL_FORMAT_WARNING)) fullFormat.setSelected(false);
        });
        erase.add(fullFormat);
        JPanel tk = new JPanel(new FlowLayout(FlowLayout.LEFT, 0, 0));
        tk.add(new JLabel("Type confirmation: "));
        tk.add(token);
        tk.add(new JLabel("  "));
        tk.add(tokenHint);
        erase.add(tk);

        JPanel bk = new JPanel(new FlowLayout(FlowLayout.LEFT));
        bk.setBorder(BorderFactory.createTitledBorder("ext4 backend (none ticked = native; selecting never installs or mounts)"));
        for (final JCheckBox b : new JCheckBox[]{wslBox, customWinBox, readonlyBox, homebrewBox, customMacBox}) {
            bk.add(b);
            b.addActionListener(e -> onBackendBox(b));
        }
        bk.add(backendLabel);
        opts.add(bk);

        JPanel acts = new JPanel(new FlowLayout(FlowLayout.LEFT));
        acts.add(button("Inspect", this::inspect, true));
        acts.add(button("Prepare / Duplicate setup", this::prepare, true));
        mountBtn.setToolTipText("Mount the selected PREPARED drive with the selected backend (manual retry if automount failed)");
        mountBtn.addActionListener(e -> mountAction());
        initBtn.addActionListener(e -> initAction());
        acts.add(mountBtn);
        acts.add(initBtn);
        acts.add(button("Unmount", this::unmount, true));

        JPanel centre = new JPanel(new BorderLayout());
        JPanel stack = new JPanel(new GridLayout(0, 1));
        stack.add(opts);
        stack.add(erase);
        centre.add(stack, BorderLayout.NORTH);
        centre.add(acts, BorderLayout.SOUTH);

        log.setEditable(false);
        log.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        JPanel main = new JPanel(new BorderLayout());
        main.add(top, BorderLayout.NORTH);
        main.add(centre, BorderLayout.CENTER);
        main.add(new JScrollPane(log), BorderLayout.SOUTH);
        frame.setContentPane(main);
        frame.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
        frame.pack();
        frame.setMinimumSize(new Dimension(800, 600));
    }

    private JMenuItem item(String label, Runnable r) {
        JMenuItem m = new JMenuItem(label);
        m.addActionListener(e -> r.run());
        return m;
    }

    private JButton button(String label, Runnable r, boolean needsDrive) {
        JButton b = new JButton(label);
        b.addActionListener(e -> r.run());
        if (needsDrive) actionButtons.add(b);
        return b;
    }

    boolean confirm(String msg) {
        return JOptionPane.showConfirmDialog(frame, msg, "Please confirm", JOptionPane.OK_CANCEL_OPTION,
                JOptionPane.WARNING_MESSAGE) == JOptionPane.OK_OPTION;
    }

    void info(String msg) { JOptionPane.showMessageDialog(frame, msg); }

    void append(final String line) {
        SwingUtilities.invokeLater(() -> { log.append(line + "\n"); log.setCaretPosition(log.getDocument().getLength()); });
    }

    // ---------------- state ----------------
    /** Apply backend gui-state JSON; preselect ONLY the verified source drive, else no selection. */
    @SuppressWarnings("unchecked")
    void applyState(Map<String, Object> st) {
        selector.removeAllItems();
        drives.clear();
        sourceDisk = st.get("source_disk") == null ? null : str(st.get("source_disk"));
        hostOs = st.get("os") == null ? "linux" : str(st.get("os"));
        boolean dataOk = "linux".equals(hostOs);
        for (int i = 0; i < dataMenu.getItemCount(); i++) if (dataMenu.getItem(i) != null) dataMenu.getItem(i).setEnabled(dataOk);
        dataMenu.setToolTipText(dataOk ? null : "Disabled: needs ext4 read/write, which is BLOCKED on this OS (see support matrix)");
        String def = st.get("default_selection") == null ? null : str(st.get("default_selection"));
        Object selected = "(select a USB drive)";
        selector.addItem(selected);
        List<Object> devs = st.get("devices") instanceof List ? (List<Object>) st.get("devices") : Collections.emptyList();
        for (Object o : devs) {
            Drive d = new Drive((Map<String, Object>) o, sourceDisk != null && sourceDisk.equals(str(((Map<String, Object>) o).get("path"))));
            selector.addItem(d);
            drives.add(d);
            if (def != null && def.equals(d.path)) selected = d;
            if (keepSelection != null && keepSelection.equals(d.path)) selected = d;
        }
        keepSelection = null;
        selector.setSelectedItem(selected);
        if (st.get("error") != null) append("detection error: " + st.get("error"));
        append(devs.isEmpty() ? "No USB storage devices detected." : devs.size() + " USB candidate(s) detected"
                + (def != null ? "; preselected the verified drive this app runs from: " + def : "; nothing preselected"));
        Object bo = st.get("backend");
        Map<String, Object> b = bo instanceof Map ? (Map<String, Object>) bo : Collections.<String, Object>emptyMap();
        applyBackend(b);
        updateTokenHint();
        updateControls();
    }

    /** Show only the boxes for this OS, tick the saved choice, show availability / RO-RW status. */
    void applyBackend(Map<String, Object> b) {
        boolean win = "windows".equals(hostOs), mac = "darwin".equals(hostOs);
        wslBox.setVisible(win); customWinBox.setVisible(win);
        readonlyBox.setVisible(mac); homebrewBox.setVisible(mac); customMacBox.setVisible(mac);
        backendChoice = b.get("choice") == null ? "native" : str(b.get("choice"));
        backendState = "linux".equals(hostOs) ? "READY" : (b.get("state") == null ? "BLOCKED" : str(b.get("state")));
        syncingBoxes = true;
        wslBox.setSelected(win && "wsl".equals(backendChoice));
        customWinBox.setSelected(win && "custom".equals(backendChoice));
        readonlyBox.setSelected(mac && "readonly".equals(backendChoice));
        homebrewBox.setSelected(mac && "homebrew".equals(backendChoice));
        customMacBox.setSelected(mac && "custom".equals(backendChoice));
        syncingBoxes = false;
        backendLabel.setText(backendText(hostOs, backendChoice, backendState, b.get("read_write")));
        if (b.get("message") != null) append("ext4 backend: " + b.get("message"));
    }

    static String backendText(String os, String choice, String state, Object rw) {
        if ("linux".equals(os)) return "ext4 backend: Linux kernel (READY, read/write)";
        String access = Boolean.TRUE.equals(rw) ? "read/write" : Boolean.FALSE.equals(rw) ? "READ-ONLY" : "access unknown";
        return "ext4 backend: " + choice + " [" + state + ", " + access + ", untested on real hardware]";
    }

    /** Mutually exclusive: ticking one unticks the others; unticking returns to native. */
    static String exclusiveChoice(String box, boolean ticked) {
        return ticked ? box : "native";
    }

    void onBackendBox(JCheckBox clicked) {
        if (syncingBoxes) return;
        String box = clicked == wslBox ? "wsl" : clicked == readonlyBox ? "readonly" : clicked == homebrewBox ? "homebrew" : "custom";
        final String choice = exclusiveChoice(box, clicked.isSelected());
        syncingBoxes = true;
        for (JCheckBox o : new JCheckBox[]{wslBox, customWinBox, readonlyBox, homebrewBox, customMacBox})
            if (o != clicked) o.setSelected(false);
        syncingBoxes = false;
        append("Backend selection -> " + choice + " (saved only; nothing is installed, run or remounted. "
                + "Unmount first, then use Mount / setup to switch.)");
        new Thread(() -> {
            try {
                StringBuilder out = new StringBuilder();
                runBackend(Arrays.asList("backend", "set", choice), out, null);
                out.setLength(0);
                runBackend(Arrays.asList("backend", "status", "--json"), out, null);
                final Map<String, Object> b = (Map<String, Object>) Json.parse(out.toString());
                SwingUtilities.invokeLater(() -> { applyBackend(b); updateControls(); });
            } catch (Exception ex) { append("backend selection failed: " + ex); }
        }, "backend-set").start();
    }

    /** Pure decision for the Mount / Initialize buttons: [mountEnabled, initLabel, initEnabled, reason]. */
    static String[] controlState(String os, Drive d, String backendState, boolean busy) {
        if (busy) return new String[]{"false", "Initialize", "false", "busy: an operation is running"};
        if (d == null) return new String[]{"false", "Initialize", "false", "select a USB drive"};
        boolean prepared = d.prepared;
        boolean usable = !"excluded".equals(d.status);
        String label = prepared ? "Re-initialize" : "Initialize";
        boolean initOk = "linux".equals(os) && usable;
        boolean mountOk = prepared && usable && "READY".equals(backendState);
        String reason = !usable ? "drive is not detected as a dedicated USB drive"
                + (d.starAllowed ? " (click the star if it is one)" : ": " + d.starRefusal)
                : !prepared ? "drive is not prepared: Mount needs a prepared drive (use Initialize)"
                : !"READY".equals(backendState) ? "selected ext4 backend is " + backendState + " on this host"
                : "ready";
        if (!initOk) reason += "; Initialize/Re-initialize run on Linux only";
        return new String[]{String.valueOf(mountOk), label, String.valueOf(initOk), reason};
    }

    void updateControls() {
        String[] c = controlState(hostOs, selected(), backendState, busy);
        mountBtn.setEnabled(Boolean.parseBoolean(c[0]));
        initBtn.setText(c[1]);
        initBtn.setEnabled(Boolean.parseBoolean(c[2]));
        mountBtn.setToolTipText(c[3]);
        initBtn.setToolTipText(c[3]);
        String[] sb = starButtonState(selected(), busy);
        starBtn.setEnabled(Boolean.parseBoolean(sb[0]));
        starBtn.setText(sb[1]);
        starBtn.setToolTipText(sb[2]);
    }

    /** Pure decision for the star toggle: [enabled, text, tooltip]. */
    static String[] starButtonState(Drive d, boolean busy) {
        if (d == null) return new String[]{"false", STAR_OFF, "select a USB drive"};
        boolean on = !d.starred.isEmpty();
        if (busy) return new String[]{"false", on ? STAR_ON : STAR_OFF, "busy: an operation is running"};
        if (!d.starAllowed) return new String[]{"false", STAR_OFF,
                d.starRefusal.isEmpty() ? "this drive cannot be starred" : d.starRefusal};
        if (on) return new String[]{"true", STAR_ON, "Marked as a dedicated USB drive"
                + ("session".equals(d.starred) ? " for this session only (no serial number)" : "") + " - click to remove the mark"};
        return new String[]{"true", STAR_OFF, "Mark this drive as a dedicated USB drive (it was "
                + (d.detected ? "" : "not ") + "detected as one)"};
    }

    /** Pure argv for the star toggle (saved marks only; session-only marks never reach the disk). */
    static List<String> starArgs(Drive d, boolean on) {
        return on ? Arrays.asList("star", d.path, "--yes") : Arrays.asList("unstar", d.path);
    }

    /** Star ON asks first (nothing runs from the dialog except saving the mark); star OFF needs no confirmation. */
    void toggleStar() {
        final Drive d = selected();
        if (d == null || !d.starAllowed || busy) return;
        keepSelection = d.path;
        if ("session".equals(d.starred)) { sessionStars.remove(d.sessionKey); refresh(); return; }
        if ("saved".equals(d.starred)) { runAsync(Collections.singletonList(starArgs(d, false)), rc -> refresh()); return; }
        if (!confirm(STAR_WARNING)) { keepSelection = null; return; }
        if (!d.serialPresent) {
            info("This drive reports no serial number, so it cannot be recognised reliably after re-plugging.\n"
                    + "The star is NOT saved: it applies to this GUI session only (shown as " + STAR_ON + " (this session)).");
            sessionStars.add(d.sessionKey);
            refresh();
            return;
        }
        runAsync(Collections.singletonList(starArgs(d, true)), rc -> refresh());
    }

    String sessionStarsJson() {
        List<String> q = new ArrayList<String>();
        for (String k : sessionStars) q.add(Json.quote(k));
        return q.toString();
    }

    /** Re-check the selected drive against a FRESH detection: same path, same serial, still prepared. null = OK. */
    @SuppressWarnings("unchecked")
    static String revalidate(Map<String, Object> fresh, Drive d, boolean needPrepared) {
        Object devs = fresh.get("devices");
        if (devs instanceof List) for (Object o : (List<Object>) devs) {
            Map<String, Object> m = (Map<String, Object>) o;
            if (!d.path.equals(str(m.get("path")))) continue;
            if (!d.serial.equals(str(m.get("serial"))))
                return "stale selection: " + d.path + " is now a different drive (serial " + str(m.get("serial")) + "); press Refresh";
            if (needPrepared && !"prepared".equals(str(m.get("status"))) && !"prepared".equals(str(m.get("layout"))))
                return d.path + " is no longer detected as prepared (" + str(m.get("status")) + ")";
            return null;
        }
        return "stale selection: " + d.path + " is no longer connected; press Refresh";
    }

    @SuppressWarnings("unchecked")
    String revalidateNow(Drive d, boolean needPrepared) {
        try {
            StringBuilder out = new StringBuilder();
            runBackend(Arrays.asList("gui-state"), out, null);
            Map<String, Object> st = (Map<String, Object>) Json.parse(out.toString());
            String err = revalidate(st, d, needPrepared);
            if (err == null && needPrepared && !"linux".equals(hostOs)) {
                Object b = st.get("backend");
                String state = b instanceof Map ? str(((Map<String, Object>) b).get("state")) : "BLOCKED";
                if (!"READY".equals(state)) err = "selected ext4 backend is " + state + " (no other backend is used instead)";
            }
            return err;
        } catch (Exception e) { return "could not re-check the drive: " + e.getMessage(); }
    }

    /** Mount: only for prepared drives with a READY backend; re-validated at click time; never falls back. */
    void mountAction() {
        final Drive d = selected();
        if (d == null) return;
        String err = revalidateNow(d, true);
        if (err != null) { info("Mount refused: " + err); refresh(); return; }
        if ("linux".equals(hostOs)) { mount(); return; }
        if ("custom".equals(backendChoice)) {
            String dev = JOptionPane.showInputDialog(frame, "Custom driver: mount point folder for " + d.path + "?");
            if (dev == null || dev.trim().isEmpty()) return;
            StringBuilder out = new StringBuilder();
            try { runBackend(Arrays.asList("backend", "status", "--json"), out, null); } catch (Exception e) { info(e.toString()); return; }
            Map<String, Object> b = (Map<String, Object>) Json.parse(out.toString());
            Object cd = b.get("custom_driver");
            String script = cd instanceof Map ? str(((Map<String, Object>) cd).get("path")) : "";
            if (!confirm("Run YOUR script?\n\n" + script + "\n\nIt runs as you (no inherited admin), is not sandboxed, and its "
                    + "exit code alone is not trusted: the mount is checked independently afterwards.")) return;
            runAsync(Collections.singletonList(Arrays.asList("backend", "custom-driver", "mount", "--device", d.path,
                    "--mount-point", dev.trim(), "--trust-script", script)), rc -> refresh());
            return;
        }
        String cmd = "darwin".equals(hostOs)
                ? ("readonly".equals(backendChoice) ? "Start/MacOS/start.command setup readonly-mount diskNsM"
                   : "Start/MacOS/start.command setup homebrew-mount diskNsM")
                : ("wsl".equals(backendChoice) ? "Start\\Windows\\START.bat setup -Action MountExt4 -UseWSL"
                   : "Start\\Windows\\START.bat setup -Action MountExt4");
        info("This backend mounts through the consent-gated setup helper (native admin prompt), run in a terminal:\n\n  "
                + cmd + "\n\n(diskNsM = the ext4 partition from 'diskutil list'.) The helper verifies the result and never "
                + "switches to another driver.");
    }

    /** Initialize = existing prepare flow; Re-initialize = existing staged reformat flow. Both keep every confirmation. */
    void initAction() {
        Drive d = selected();
        if (d == null) return;
        boolean prepared = d.prepared;
        String err = revalidateNow(d, false);
        if (err != null) { info(initBtn.getText() + " refused: " + err); refresh(); return; }
        if (prepared) reformat(); else prepare();
    }

    Drive selected() {
        Object o = selector.getSelectedItem();
        return o instanceof Drive ? (Drive) o : null;
    }

    private void updateTokenHint() {
        Drive d = selected();
        tokenHint.setText(d == null ? " " : "(type exactly: " + d.token + ")");
    }

    @SuppressWarnings("unchecked")
    void refresh() {
        new Thread(() -> {
            try {
                StringBuilder out = new StringBuilder();
                int rc = runBackend(Arrays.asList("gui-state"), out, null);
                final Map<String, Object> st = (Map<String, Object>) Json.parse(out.toString());
                SwingUtilities.invokeLater(() -> applyState(st));
                refreshScripts();
                if (rc != 0) append("gui-state exit " + rc);
            } catch (Exception ex) {
                append("Could not query the backend: " + ex + "\nPython 3 is required for drive operations (see START_HERE.txt).");
            }
        }, "refresh").start();
    }

    // ---------------- argument building (pure; covered by --self-test) ----------------
    List<String> common(List<String> a) {
        List<String> r = new ArrayList<String>(a);
        if (allowAmbiguous.isSelected()) r.add("--allow-ambiguous");
        if (!dryRun.isSelected()) {
            r.add("--execute");
            String p = (String) elevation.getSelectedItem();
            if (!"none".equals(p)) { r.add("--privilege"); r.add(p); }
        }
        return r;
    }

    /** Returns argv, or throws IllegalStateException with the reason the action is refused. */
    List<String> destructiveArgs(String cmd, Drive d) {
        List<String> r = common(Arrays.asList(cmd, d.path));
        if (!dryRun.isSelected()) {
            if (!eraseAck.isSelected()) throw new IllegalStateException("tick the erase acknowledgement checkbox first");
            if (!d.token.equals(token.getText().trim()))
                throw new IllegalStateException("typed confirmation must be exactly '" + d.token + "'");
            r.add("--confirm");
            r.add(d.token);
        }
        if (fullFormat.isSelected()) r.add("--full-format");
        return r;
    }

    String formatMode() {
        return fullFormat.isSelected() ? "FULL format: every partition is zero-filled (slow, wears flash; not a secure erase)"
                                       : "Quick format: filesystems are created; old data blocks are not overwritten";
    }

    /** Clone confirmation token for the destination: REPLACE + last 4 of its erase token. */
    static String cloneToken(Drive dst) {
        String t = dst.token;
        return "REPLACE " + (t.contains(" ") ? t.substring(t.indexOf(' ') + 1) : t);
    }

    /** Pure argv builder for Clone; throws IllegalStateException with the refusal reason. */
    List<String> cloneArgs(Drive src, Drive dst, String typed) {
        if (src == null || dst == null) throw new IllegalStateException("choose both a source and a destination USB");
        if (src.path.equals(dst.path)) throw new IllegalStateException("source and destination are the same drive");
        if (dst.source) throw new IllegalStateException("the destination is the USB this app runs from; refused");
        List<String> r = new ArrayList<String>(Arrays.asList("clone", src.path, dst.path));
        if (allowAmbiguous.isSelected()) r.add("--allow-ambiguous");
        if (typed != null) {
            if (!cloneToken(dst).equals(typed.trim()))
                throw new IllegalStateException("typed confirmation must be exactly '" + cloneToken(dst) + "'");
            r.add("--execute");
            r.add("--confirm");
            r.add(cloneToken(dst));
            String p = (String) elevation.getSelectedItem();
            if (!"none".equals(p)) { r.add("--privilege"); r.add(p); }
        }
        return r;
    }

    /** Pure argv builder for running one user-selected script (never elevated, explicit trust flag). */
    static List<String> scriptArgs(String name, Drive d) {
        List<String> r = new ArrayList<String>(Arrays.asList("scripts", "run", name));
        if (d != null) { r.add("--device"); r.add(d.path); }
        r.add("--i-trust-this-script");
        return r;
    }

    // ---------------- actions ----------------
    Drive requireDrive() {
        Drive d = selected();
        if (d == null) info("Select a USB drive in the dropdown first (nothing is ever auto-selected except the "
                + "verified drive this app runs from).");
        return d;
    }

    void inspect() {
        Drive d = requireDrive();
        if (d != null) runAsync(Arrays.asList(Arrays.asList("inspect", d.path, "--allow-ambiguous")), null);
    }

    void prepare() {
        Drive d = requireDrive();
        if (d == null) return;
        if (d.source) { info("This is the drive the app runs from. It is protected here; use Extras > Reformat."); return; }
        String cmd = sourceDisk != null ? "duplicate" : "prepare";
        try {
            List<String> a = destructiveArgs(cmd, d);
            if ("duplicate".equals(cmd)) {
                String choice = askBootContent();
                if (choice == null) { append("Cancelled - nothing was written."); return; }
                if (!choice.isEmpty()) { a.add("--boot-content"); a.add(choice); }
            }
            if (!dryRun.isSelected() && !confirm("ERASE " + d.label + "?\nAll data on it will be deleted (an already "
                    + "prepared drive is preserved untouched).\n" + formatMode())) return;
            runAsync(Collections.singletonList(a), null);
        } catch (IllegalStateException ex) { info(ex.getMessage()); }
    }

    static final String[] BOOT_CHOICES = {"Preserve user updates", "Create a fresh installation", "Cancel"};

    /** Maps the dialog answer to the backend flag; null = cancel (no disk writes). */
    static String bootChoiceArg(int answer) {
        return answer == 0 ? "preserve" : answer == 1 ? "fresh" : null;
    }

    /** "" = source boot example unmodified (stock is used, no question); null = cancelled; else preserve|fresh. */
    String askBootContent() {
        StringBuilder out = new StringBuilder();
        try {
            if (runBackend(Arrays.asList("boot-status"), out, null) != 0) return null;
        } catch (Exception e) { info("Could not read the boot example status: " + e.getMessage()); return null; }
        Map<String, Object> st = (Map<String, Object>) Json.parse(out.toString());
        if (!Boolean.TRUE.equals(st.get("modified"))) return "";
        int ans = JOptionPane.showOptionDialog(frame,
                "The boot example on this USB (AI/scr/BIOS, AI/src/EFI) has your changes.\n"
                + "What should the NEW drive get? (This USB is never modified.)\n\n"
                + "Preserve user updates: copy your sources; boot code is installed only if its build matches them.\n"
                + "Create a fresh installation: the stock Hello World example.",
                "Boot example", JOptionPane.DEFAULT_OPTION, JOptionPane.QUESTION_MESSAGE, null, BOOT_CHOICES,
                BOOT_CHOICES[2]);
        return bootChoiceArg(ans);
    }

    void mount() {
        Drive d = requireDrive();
        if (d == null) return;
        List<List<String>> steps = new ArrayList<List<String>>();
        if (scanBeforeMount.isSelected()) {
            List<String> c = common(Arrays.asList("check", d.path, "--scan-bad-blocks"));
            if (fullRepair.isSelected()) c.add("--full-repair");
            steps.add(c);
        }
        List<String> m = common(Arrays.asList("mount", d.path));
        if (updateApp.isSelected()) m.add("--update-app");
        if (fullRepair.isSelected()) m.add("--full-repair");
        steps.add(m);
        runAsync(steps, null);
    }

    void unmount() {
        Drive d = requireDrive();
        if (d == null) return;
        if (d.source) { info("Refused: the app runs from this drive. Close the app, then eject it from your desktop."); return; }
        List<String> a = new ArrayList<String>(Arrays.asList("unmount", d.path));
        if (!dryRun.isSelected()) {
            a.add("--execute");
            String p = (String) elevation.getSelectedItem();
            if (!"none".equals(p)) { a.add("--privilege"); a.add(p); }
        }
        runAsync(Collections.singletonList(a), null);
    }

    void checkDrive() {
        Drive d = requireDrive();
        if (d != null) runAsync(Collections.singletonList(common(Arrays.asList("check-drive", d.path))), null);
    }

    void repairDrive() {
        Drive d = requireDrive();
        if (d == null) return;
        if (!dryRun.isSelected() && !confirm("Run the safe automatic ext4 repair (e2fsck -p) on " + d.path + "?\n"
                + (fullRepair.isSelected() ? "You consented to a FULL repair (-f -y) if needed; it may delete damaged files."
                                           : "A full repair will NOT run without the consent option.")
                + "\nFAT32 and swap are not repaired automatically.")) return;
        List<String> a = common(Arrays.asList("repair", d.path));
        if (fullRepair.isSelected()) a.add("--full-repair");
        runAsync(Collections.singletonList(a), null);
    }

    void scanNow() {
        Drive d = requireDrive();
        if (d == null) return;
        int r = JOptionPane.showConfirmDialog(frame, "Read-only bad-block scan of the AI (ext4) partition on " + d.path
                + ".\nThe partition must be unmounted. Nothing is written unless you consent below.\n\n"
                + "Also record unreadable blocks in the ext4 bad-block list (e2fsck -l)?\n"
                + "This does NOT repair failing flash; back up and replace the drive if errors are found.",
                M_SCAN_NOW, JOptionPane.YES_NO_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE);
        if (r == JOptionPane.CANCEL_OPTION || r == JOptionPane.CLOSED_OPTION) return;
        markConsent = r == JOptionPane.YES_OPTION;
        List<String> a = common(Arrays.asList("check", d.path, "--scan-bad-blocks"));
        if (markConsent) a.add("--mark-bad-blocks");
        if (fullRepair.isSelected()) a.add("--full-repair");
        runAsync(Collections.singletonList(a), null);
    }

    void reformat() {
        Drive d = requireDrive();
        if (d == null) return;
        String warn = "REFORMAT " + d.label + "\n\nThe setup software in EFI:/AI (and its non-secret config) is backed up\n"
                + "to a private /tmp folder, the app relaunches from there, the drive is re-partitioned and\n"
                + "formatted, then the software is restored and verified.\n\n"
                + "ext4 (AI partition) user data WILL BE ERASED and is NOT backed up.\n"
                + "Files outside EFI:/AI (e.g. a bootloader you installed under EFI/) are also erased.\n"
                + "This window closes during the reformat; a progress window opens from the /tmp copy.\n"
                + formatMode();
        try {
            List<String> a = destructiveArgs("reformat", d);
            if (!confirm(warn)) return;
            if (!dryRun.isSelected()) { a.add("--wait-pid"); a.add(pid()); a.add("--monitor"); }
            runAsync(Collections.singletonList(a), rc -> {
                if (rc == 0 && !dryRun.isSelected() && log.getText().contains("HANDOFF-OK")) {
                    // Our jar lives on the target drive: exit so no handle/cwd remains on it.
                    info("Backup verified and handoff confirmed. This window now closes; the reformat continues from /tmp.");
                    System.exit(0);
                }
            });
        } catch (IllegalStateException ex) { info(ex.getMessage()); }
    }

    void vmdk() {
        Drive d = requireDrive();
        if (d == null) return;
        String warn = "Create a VirtualBox raw-disk descriptor (.vmdk) for the WHOLE drive " + d.label + "\n"
                + "in EFI:/AI/Cache/Virtual.\n\n"
                + "* It is a small POINTER file, not a copy: the USB must stay connected.\n"
                + "* VirtualBox will read/write the real drive. Never use the drive on this computer and in the VM\n"
                + "  at the same time (unmount it here first) - simultaneous access corrupts data.\n"
                + "* The device path and permissions are specific to this computer; regenerate it elsewhere.\n"
                + "* The VM boots only from a bootloader YOU install in the EFI partition (enable UEFI in the VM).\n"
                + "No VM is created, registered, started or attached.";
        if (!confirm(warn)) return;
        List<String> a = common(Arrays.asList("vmdk", d.path, "--confirm-raw-disk"));
        int r = JOptionPane.showConfirmDialog(frame, "If an older usbprep descriptor for this drive exists, regenerate it?",
                M_VMDK, JOptionPane.YES_NO_CANCEL_OPTION);
        if (r == JOptionPane.CANCEL_OPTION || r == JOptionPane.CLOSED_OPTION) return;
        if (r == JOptionPane.YES_OPTION) a.add("--replace");
        runAsync(Collections.singletonList(a), null);
    }

    void vmHandoff() {
        Drive d = requireDrive();
        if (d == null) return;
        if (!confirm("VirtualBox does NOT unmount the drive by itself when a VM boots from the .vmdk.\n"
                + "This step releases " + d.path + " from this computer: unmounts /USB/A.I. (ext4) and /USB (FAT32),\n"
                + "turns its swap off and flushes buffers. Close files open on the drive first.\n"
                + "Then start YOUR VM yourself. Do not also add a USB filter for this drive.")) return;
        String vm = JOptionPane.showInputDialog(frame, "Optional: VM name (it must be powered off now). Leave empty to skip.");
        if (vm == null) return;
        List<String> a = common(Arrays.asList("vm-handoff", d.path));
        if (!vm.trim().isEmpty()) { a.add("--vm"); a.add(vm.trim()); }
        runAsync(Collections.singletonList(a), null);
    }

    void vmReturn() {
        Drive d = requireDrive();
        if (d == null) return;
        String vm = JOptionPane.showInputDialog(frame, "VM name to verify it is powered off (or leave empty and confirm next):");
        if (vm == null) return;
        List<String> a = common(Arrays.asList("vm-return", d.path));
        if (!vm.trim().isEmpty()) { a.add("--vm"); a.add(vm.trim()); }
        else if (confirm("Is the VM that used this drive FULLY powered off (not saved, not paused)?")) a.add("--vm-powered-off");
        else return;
        runAsync(Collections.singletonList(a), null);
    }

    void showMatrix() {
        new Thread(() -> {
            StringBuilder out = new StringBuilder();
            try { runBackend(Arrays.asList("capabilities"), out, null); }
            catch (Exception ex) { out.append("backend unavailable: ").append(ex); }
            SwingUtilities.invokeLater(() -> {
                JTextArea t = new JTextArea(out.toString(), 30, 110);
                t.setEditable(false);
                JOptionPane.showMessageDialog(frame, new JScrollPane(t), M_MATRIX, JOptionPane.PLAIN_MESSAGE);
            });
        }, "matrix").start();
    }

    // ---------------- Clone ----------------
    void cloneDialog() {
        if (drives.size() < 2) { info("Clone needs two prepared USB drives connected (press Refresh after plugging them in)."); return; }
        JComboBox<Object> src = new JComboBox<Object>(), dst = new JComboBox<Object>();
        src.addItem("(choose source)"); dst.addItem("(choose destination)");
        for (Drive d : drives) { src.addItem(d); dst.addItem(d); }
        JPanel p = new JPanel(new GridLayout(0, 1));
        p.add(new JLabel("Copies ONLY the ext4 'A.I.' partition contents (rsync, file level). FAT32 and swap are not touched."));
        p.add(new JLabel("Source (read only, never modified):")); p.add(src);
        p.add(new JLabel("Destination (its A.I. partition files are REPLACED):")); p.add(dst);
        p.add(new JLabel("Both drives must be prepared and their ext4 partitions unmounted. A dry run is shown first."));
        if (JOptionPane.showConfirmDialog(frame, p, M_CLONE, JOptionPane.OK_CANCEL_OPTION) != JOptionPane.OK_OPTION) return;
        final Drive s = src.getSelectedItem() instanceof Drive ? (Drive) src.getSelectedItem() : null;
        final Drive d = dst.getSelectedItem() instanceof Drive ? (Drive) dst.getSelectedItem() : null;
        try {
            List<String> dry = cloneArgs(s, d, null);
            runAsync(Collections.singletonList(dry), rc -> {
                if (rc != 0 || dryRun.isSelected()) return;
                String typed = JOptionPane.showInputDialog(frame, "ALL files on the A.I. (ext4) partition of\n" + d.label
                        + "\nwill be REPLACED by the files from " + s.path + ".\nFAT32, swap and filesystem UUIDs are not changed.\n\n"
                        + "Type exactly: " + cloneToken(d));
                if (typed == null) { append("Clone cancelled - nothing was written."); return; }
                try { runAsync(Collections.singletonList(cloneArgs(s, d, typed)), null); }
                catch (IllegalStateException ex) { info(ex.getMessage()); }
            });
        } catch (IllegalStateException ex) { info(ex.getMessage()); }
    }

    // ---------------- Data ----------------
    /** Pure argv builders for the Data menu (backend does all checks; dry run unless executing). */
    static List<String> dataBackupArgs(Drive d, boolean execute) {
        List<String> r = new ArrayList<String>(Arrays.asList("data", "backup", "--device", d.path, "--json"));
        if (execute) { r.add("--execute"); r.add("--i-understand-777"); }
        return r;
    }

    static List<String> dataRestoreArgs(Drive d, boolean shared, String id, String conflict, String typed) {
        List<String> r = new ArrayList<String>(Arrays.asList("data", shared ? "restore-shared" : "restore", "--device",
                d.path, id, "--conflict", conflict, "--json"));
        if (!shared && !id.startsWith("Linux/")) r.add("--allow-foreign-os");
        if (typed != null) { r.add("--execute"); r.add("--confirm"); r.add(typed.trim()); }
        return r;
    }

    boolean dataAllowed() {
        if (!"linux".equals(hostOs)) {
            info("Data is disabled on this OS: it needs read/write access to the USB ext4 partition, which is BLOCKED here.");
            return false;
        }
        return true;
    }

    void dataList() {
        Drive d = requireDrive();
        if (d != null && dataAllowed()) runAsync(Collections.singletonList(Arrays.asList("data", "list", "--device", d.path)), null);
    }

    void dataBackup() {
        final Drive d = requireDrive();
        if (d == null || !dataAllowed()) return;
        runAsync(Collections.singletonList(dataBackupArgs(d, false)), rc -> {
            if (rc != 0 || dryRun.isSelected()) return;
            SwingUtilities.invokeLater(() -> {
                if (confirm(DATA_WARNING)) runAsync(Collections.singletonList(dataBackupArgs(d, true)), null);
                else append("Backup cancelled - nothing was written.");
            });
        });
    }

    void dataRestore(final boolean shared) {
        final Drive d = requireDrive();
        if (d == null || !dataAllowed()) return;
        final String id = JOptionPane.showInputDialog(frame, shared
                ? "Shared source to restore (machine/user, from 'List backups', e.g. 3fa1c2.../alice):"
                : "OS backup to restore (OS/machine/user, from 'List backups'; other OSes are copied as files only):");
        if (id == null || id.trim().isEmpty()) return;
        final String conflict = (String) JOptionPane.showInputDialog(frame, "Existing files in your home:", "Conflicts",
                JOptionPane.QUESTION_MESSAGE, null, new String[]{"skip-newer", "skip-existing", "overwrite"}, "skip-newer");
        if (conflict == null) return;
        runAsync(Collections.singletonList(dataRestoreArgs(d, shared, id.trim(), conflict, null)), rc -> {
            if (rc != 0 || dryRun.isSelected()) return;
            SwingUtilities.invokeLater(() -> {
                String typed = JOptionPane.showInputDialog(frame, "The preview above lists what will be created and "
                        + "OVERWRITTEN in your home (nothing is deleted).\nType the confirmation token from the preview "
                        + "(RESTORE <your user name>):");
                if (typed == null) { append("Restore cancelled - nothing was written."); return; }
                runAsync(Collections.singletonList(dataRestoreArgs(d, shared, id.trim(), conflict, typed)), null);
            });
        });
    }

    // ---------------- Scripts ----------------
    /** Rebuild the Scripts menu from `scripts list --json` output (null = not loaded yet). Never runs anything. */
    @SuppressWarnings("unchecked")
    void populateScripts(Map<String, Object> list) {
        scriptsMenu.removeAll();
        scriptsMenu.add(item(M_SCRIPTS_REFRESH, new Runnable() { public void run() { refreshScripts(); } }));
        scriptsMenu.addSeparator();
        List<Object> sc = list != null && list.get("scripts") instanceof List ? (List<Object>) list.get("scripts") : Collections.emptyList();
        if (sc.isEmpty()) {
            JMenuItem none = new JMenuItem(list == null ? "(press Refresh script list)" : "(no scripts in " + str(list.get("dir")) + ")");
            none.setEnabled(false);
            scriptsMenu.add(none);
        }
        for (Object o : sc) {
            Map<String, Object> m = (Map<String, Object>) o;
            final String name = str(m.get("name"));
            final JMenuItem it = new JMenuItem(name);
            if (!Boolean.TRUE.equals(m.get("available"))) {
                it.setToolTipText("needs " + str(m.get("interpreter")) + " (not installed)");
            }
            it.addActionListener(e -> runScript(name, it));
            scriptsMenu.add(it);
        }
    }

    @SuppressWarnings("unchecked")
    void refreshScripts() {
        new Thread(() -> {
            StringBuilder out = new StringBuilder();
            try {
                runBackend(Arrays.asList("scripts", "list", "--json"), out, null);
                final Map<String, Object> m = (Map<String, Object>) Json.parse(out.toString());
                SwingUtilities.invokeLater(() -> populateScripts(m));
            } catch (Exception ex) { append("Could not list scripts: " + ex); }
        }, "scripts").start();
    }

    void runScript(final String name, final JMenuItem it) {
        if (!runningScripts.add(name)) { info("'" + name + "' is already running."); return; }
        if (!confirm("Run the script '" + name + "'?\n\nScripts can run any command with your user rights. usbprep does "
                + "not check what a script does\nand cannot guarantee its behaviour. Run it only if you trust it.\n"
                + "It runs as your normal user (never with the app's formatting/admin rights).")) {
            runningScripts.remove(name);
            return;
        }
        it.setEnabled(false);
        final List<String> a = scriptArgs(name, selected());
        new Thread(() -> {
            append("$ usbprep " + String.join(" ", a));
            int rc;
            try { rc = runBackend(a, null, this::append); } catch (Exception ex) { append("script failed: " + ex); rc = 1; }
            append("(script exit " + rc + ")");
            runningScripts.remove(name);
            SwingUtilities.invokeLater(() -> it.setEnabled(true));
        }, "script").start();
    }

    static String pid() { return ManagementFactory.getRuntimeMXBean().getName().split("@")[0]; }

    // ---------------- backend process ----------------
    interface Done { void done(int rc); }

    void runAsync(final List<List<String>> steps, final Done done) {
        for (JButton b : actionButtons) b.setEnabled(false);
        busy = true;
        updateControls();
        new Thread(() -> {
            int rc = 0;
            for (List<String> a : steps) {
                append("$ usbprep " + String.join(" ", a));
                try { rc = runBackend(a, null, this::append); }
                catch (Exception ex) { append("backend failed: " + ex); rc = 1; }
                append("(exit " + rc + ")" + (dryRun.isSelected() ? "  - dry run; untick 'Dry run' to execute" : ""));
                if (rc != 0) break;
            }
            final int frc = rc;
            SwingUtilities.invokeLater(() -> {
                for (JButton b : actionButtons) b.setEnabled(true);
                busy = false;
                updateControls();
                if (done != null) done.done(frc);
            });
        }, "backend").start();
    }

    interface Sink { void line(String s); }

    int runBackend(List<String> args, StringBuilder capture, Sink sink) throws Exception {
        List<String> cmd = new ArrayList<String>(python);
        cmd.add("-m");
        cmd.add("usbprep");
        cmd.addAll(args);
        ProcessBuilder pb = new ProcessBuilder(cmd).directory(root).redirectErrorStream(capture == null);
        pb.environment().put("PYTHONPATH", root.getAbsolutePath());
        pb.environment().put("PYTHONDONTWRITEBYTECODE", "1");
        pb.environment().put("USBPREP_SESSION_STARS", sessionStarsJson());
        Process p = pb.start();
        p.getOutputStream().close();
        BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), Charset.forName("UTF-8")));
        String line;
        while ((line = r.readLine()) != null) {
            if (capture != null) capture.append(line).append('\n');
            if (sink != null) sink.line(line);
        }
        return p.waitFor();
    }

    static List<String> findPython() {
        String env = System.getenv("USBPREP_PYTHON");
        List<List<String>> cands = new ArrayList<List<String>>();
        if (env != null && !env.trim().isEmpty()) cands.add(Arrays.asList(env.trim().split("\\s+")));
        cands.add(Arrays.asList("python3"));
        cands.add(Arrays.asList("python"));
        cands.add(Arrays.asList("py", "-3"));
        for (List<String> c : cands) {
            try {
                List<String> v = new ArrayList<String>(c);
                v.add("--version");
                Process p = new ProcessBuilder(v).redirectErrorStream(true).start();
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream(), Charset.forName("UTF-8")));
                String out = r.readLine();
                if (p.waitFor() == 0 && out != null && out.startsWith("Python 3")) return c;
            } catch (Exception ignored) { /* try next */ }
        }
        return null;
    }

    static File appRoot() throws Exception {
        String env = System.getenv("USBPREP_ROOT");
        if (env != null && !env.isEmpty()) return new File(env).getAbsoluteFile();
        File jar = new File(UsbPrepGui.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        return jar.getParentFile().getParentFile().getParentFile();   // <root>/gui-java/dist/usbprep-gui.jar
    }

    // ---------------- reformat progress monitor (runs from the /tmp copy) ----------------
    static void monitor(final String logPath) {
        final JFrame f = new JFrame("USB Reformat - progress (running from the temporary copy)");
        final JTextArea t = new JTextArea(25, 100);
        t.setEditable(false);
        t.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        f.add(new JScrollPane(t));
        f.setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
        f.pack();
        f.setVisible(true);
        final long[] pos = {0};
        new Timer(500, e -> {
            try (RandomAccessFile raf = new RandomAccessFile(logPath, "r")) {
                if (raf.length() > pos[0]) {
                    byte[] b = new byte[(int) (raf.length() - pos[0])];
                    raf.seek(pos[0]);
                    raf.readFully(b);
                    pos[0] += b.length;
                    t.append(new String(b, Charset.forName("UTF-8")));
                }
            } catch (Exception ex) { /* log appears shortly; the backup folder may be removed at the end */ }
        }).start();
    }

    // ---------------- self test (headless-display check of defaults; used by pytest via xvfb-run) ----------------
    @SuppressWarnings("unchecked")
    static String selfTest(File stateFile) throws Exception {
        UsbPrepGui g = new UsbPrepGui(new File("."), Arrays.asList("python3"));
        String json = new String(Files.readAllBytes(stateFile.toPath()), Charset.forName("UTF-8"));
        g.applyState((Map<String, Object>) Json.parse(json));
        JMenu ex = g.frame.getJMenuBar().getMenu(0);
        List<String> labels = new ArrayList<String>();
        for (int i = 0; i < ex.getItemCount(); i++) if (ex.getItem(i) != null) labels.add(Json.quote(ex.getItem(i).getText()));
        Drive d = g.selected();
        StringBuilder o = new StringBuilder("{");
        o.append("\"java\":").append(Json.quote(System.getProperty("java.version")));
        o.append(",\"menu\":").append(Json.quote(ex.getText()));
        o.append(",\"items\":").append(labels);
        o.append(",\"scan_before_mount_default\":").append(g.scanBeforeMount.isSelected());
        o.append(",\"dry_run_default\":").append(g.dryRun.isSelected());
        o.append(",\"erase_ack_default\":").append(g.eraseAck.isSelected());
        o.append(",\"full_repair_default\":").append(g.fullRepair.isSelected());
        o.append(",\"selected\":").append(Json.quote(d == null ? null : d.path));
        o.append(",\"selected_is_source\":").append(d != null && d.source);
        if (d != null) {
            o.append(",\"dry_prepare\":").append(Json.quote(String.join(" ", g.destructiveArgs("prepare", d))));
            g.dryRun.setSelected(false);
            o.append(",\"exec_without_ack\":").append(Json.quote(refusal(g, d)));
            g.eraseAck.setSelected(true);
            g.token.setText("ERASE XXXX-wrong");
            o.append(",\"exec_wrong_token\":").append(Json.quote(refusal(g, d)));
            g.token.setText(d.token);
            o.append(",\"exec_ok\":").append(Json.quote(String.join(" ", g.destructiveArgs("reformat", d))));
        }
        o.append(",\"boot_choices\":[").append(Json.quote(BOOT_CHOICES[0])).append(",").append(Json.quote(BOOT_CHOICES[1]))
         .append(",").append(Json.quote(BOOT_CHOICES[2])).append("]");
        o.append(",\"boot_choice_args\":[").append(Json.quote(bootChoiceArg(0))).append(",").append(Json.quote(bootChoiceArg(1)))
         .append(",").append(Json.quote(bootChoiceArg(2))).append(",").append(Json.quote(bootChoiceArg(-1))).append("]");
        JMenuBar mb = g.frame.getJMenuBar();
        List<String> menus = new ArrayList<String>();
        for (int i = 0; i < mb.getMenuCount(); i++) menus.add(Json.quote(mb.getMenu(i).getText()));
        o.append(",\"menus\":").append(menus);
        o.append(",\"clone_items\":[").append(Json.quote(mb.getMenu(1).getItem(0).getText())).append("]");
        List<String> ditems = new ArrayList<String>();
        for (int i = 0; i < g.dataMenu.getItemCount(); i++)
            ditems.add(Json.quote(g.dataMenu.getItem(i).getText() + (g.dataMenu.getItem(i).isEnabled() ? "" : " [disabled]")));
        o.append(",\"data_items\":").append(ditems);
        if (d != null) {
            o.append(",\"data_backup_dry\":").append(Json.quote(String.join(" ", dataBackupArgs(d, false))));
            o.append(",\"data_backup_exec\":").append(Json.quote(String.join(" ", dataBackupArgs(d, true))));
            o.append(",\"data_restore_exec\":").append(Json.quote(String.join(" ",
                    dataRestoreArgs(d, false, "Windows/m1/alice", "skip-newer", "RESTORE alice"))));
        }
        o.append(",\"full_format_label\":").append(Json.quote(g.fullFormat.getText()));
        g.populateScripts((Map<String, Object>) Json.parse("{\"dir\":\"x\",\"scripts\":[{\"name\":\"my script \u00e9.sh\","
                + "\"interpreter\":\"sh\",\"available\":true},{\"name\":\"open_terminal_in_ext4.sh\",\"interpreter\":\"sh\","
                + "\"available\":true}]}"));
        List<String> sitems = new ArrayList<String>();
        for (int i = 0; i < g.scriptsMenu.getItemCount(); i++)
            if (g.scriptsMenu.getItem(i) != null) sitems.add(Json.quote(g.scriptsMenu.getItem(i).getText()));
        o.append(",\"script_items\":").append(sitems);
        o.append(",\"scripts_running_after_populate\":").append(g.runningScripts.size());
        o.append(",\"script_args\":").append(Json.quote(String.join("|", scriptArgs("my script \u00e9.sh", d))));
        if (d != null) {
            g.fullFormat.setSelected(true);
            o.append(",\"exec_ok_full\":").append(Json.quote(String.join(" ", g.destructiveArgs("reformat", d))));
            g.fullFormat.setSelected(false);
        }
        if (g.drives.size() >= 2) {
            Drive a0 = g.drives.get(0), a1 = g.drives.get(1);
            Drive s0 = a0.source ? a0 : a1, t0 = a0.source ? a1 : a0;
            o.append(",\"clone_token\":").append(Json.quote(cloneToken(t0)));
            o.append(",\"clone_dry\":").append(Json.quote(String.join(" ", g.cloneArgs(s0, t0, null))));
            o.append(",\"clone_exec\":").append(Json.quote(String.join(" ", g.cloneArgs(s0, t0, cloneToken(t0)))));
            o.append(",\"clone_same\":").append(Json.quote(cloneRefusal(g, s0, s0, null)));
            o.append(",\"clone_to_source\":").append(Json.quote(cloneRefusal(g, t0, s0, null)));
            o.append(",\"clone_wrong_token\":").append(Json.quote(cloneRefusal(g, s0, t0, "REPLACE 0000x")));
        }
        o.append(",\"backend_boxes\":{\"wsl\":[").append(g.wslBox.isVisible()).append(",").append(g.wslBox.isSelected())
         .append("],\"custom_win\":[").append(g.customWinBox.isVisible()).append(",").append(g.customWinBox.isSelected())
         .append("],\"readonly\":[").append(g.readonlyBox.isVisible()).append(",").append(g.readonlyBox.isSelected())
         .append("],\"homebrew\":[").append(g.homebrewBox.isVisible()).append(",").append(g.homebrewBox.isSelected())
         .append("],\"custom_mac\":[").append(g.customMacBox.isVisible()).append(",").append(g.customMacBox.isSelected()).append("]}");
        o.append(",\"backend_label\":").append(Json.quote(g.backendLabel.getText()));
        o.append(",\"mount_enabled\":").append(g.mountBtn.isEnabled());
        o.append(",\"init_label\":").append(Json.quote(g.initBtn.getText()));
        o.append(",\"init_enabled\":").append(g.initBtn.isEnabled());
        // pure decision table for prepared / unprepared / busy / backend states
        Map<String, Object> up = new java.util.HashMap<String, Object>();
        up.put("path", "/dev/sdz"); up.put("serial", "ZZZ9"); up.put("status", "blank");
        Drive un = new Drive(up, false);
        List<String> cs = new ArrayList<String>();
        for (Object[] c : new Object[][]{{"linux", d, "READY", false}, {"linux", un, "READY", false}, {"linux", d, "READY", true},
                {"windows", d, "MISSING-DEPENDENCY", false}, {"darwin", d, "READY", false}, {"darwin", un, "READY", false},
                {"linux", null, "READY", false}}) {
            String[] r = controlState((String) c[0], (Drive) c[1], (String) c[2], (Boolean) c[3]);
            cs.add(Json.quote(r[0] + "|" + r[1] + "|" + r[2]));
        }
        o.append(",\"control_table\":").append(cs);
        o.append(",\"exclusive\":[").append(Json.quote(exclusiveChoice("wsl", true))).append(",")
         .append(Json.quote(exclusiveChoice("readonly", false))).append("]");
        if (d != null) {
            String ok = revalidate((Map<String, Object>) Json.parse(json), d, true);
            Map<String, Object> st2 = (Map<String, Object>) Json.parse(json.replace("\"" + d.serial + "\"", "\"SWAPPED\""));
            Map<String, Object> st3 = (Map<String, Object>) Json.parse("{\"devices\":[]}");
            o.append(",\"revalidate\":[").append(Json.quote(ok == null ? "OK" : ok)).append(",")
             .append(Json.quote(String.valueOf(revalidate(st2, d, true)))).append(",")
             .append(Json.quote(String.valueOf(revalidate(st3, d, true)))).append("]");
        }
        // star: per-drive button state + label for every listed drive, then simulated toggle via a fresh state
        List<String> stars = new ArrayList<String>();
        for (Drive x : g.drives) {
            g.selector.setSelectedItem(x);
            String[] sb = starButtonState(x, false);
            String[] cx = controlState(g.hostOs, x, "READY", false);
            stars.add("{\"path\":" + Json.quote(x.path) + ",\"enabled\":" + g.starBtn.isEnabled() + ",\"text\":"
                    + Json.quote(g.starBtn.getText()) + ",\"tooltip\":" + Json.quote(g.starBtn.getToolTipText())
                    + ",\"pure\":" + Json.quote(sb[0] + "|" + sb[1]) + ",\"label\":" + Json.quote(x.label)
                    + ",\"mount\":" + cx[0] + ",\"init\":" + cx[2] + ",\"init_label\":" + Json.quote(cx[1])
                    + ",\"args_on\":" + Json.quote(String.join(" ", starArgs(x, true)))
                    + ",\"args_off\":" + Json.quote(String.join(" ", starArgs(x, false))) + "}");
        }
        o.append(",\"stars\":").append(stars);
        o.append(",\"star_warning\":").append(Json.quote(STAR_WARNING));
        o.append(",\"star_none\":").append(Json.quote(String.join("|", starButtonState(null, false))));
        g.sessionStars.add("session|/dev/sdq|x|y|1");
        o.append(",\"session_env\":").append(Json.quote(g.sessionStarsJson()));
        g.frame.dispose();
        return o.append("}").toString();
    }

    private static String cloneRefusal(UsbPrepGui g, Drive s, Drive d, String typed) {
        try { return "ALLOWED " + String.join(" ", g.cloneArgs(s, d, typed)); }
        catch (IllegalStateException e) { return "REFUSED " + e.getMessage(); }
    }

    private static String refusal(UsbPrepGui g, Drive d) {
        try { return "ALLOWED " + String.join(" ", g.destructiveArgs("prepare", d)); }
        catch (IllegalStateException e) { return "REFUSED " + e.getMessage(); }
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 2 && "--self-test".equals(args[0])) {
            System.out.println(selfTest(new File(args[1])));
            System.exit(0);
        }
        if (args.length == 2 && "--monitor".equals(args[0])) {
            SwingUtilities.invokeLater(() -> monitor(args[1]));
            return;
        }
        final File root = appRoot();
        final List<String> py = findPython();
        SwingUtilities.invokeLater(() -> {
            UsbPrepGui g = new UsbPrepGui(root, py == null ? Arrays.asList("python3") : py);
            g.frame.setVisible(true);
            g.append("Project 1 USB Prep - Java " + System.getProperty("java.version") + " - app folder " + root);
            g.append("Dry run is ON by default. Bad-block scanning is OFF unless enabled in the Extras menu.");
            if (py == null) {
                g.append("Python 3 was not found: drive operations need it (Linux: install python3). "
                        + "Set USBPREP_PYTHON to override. See START_HERE.txt.");
            } else {
                g.refresh();
            }
        });
    }
}

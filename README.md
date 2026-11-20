# BTLEAdvFix

Experimental Lilu plugin that restores Bluetooth LE advertising on Hackintoshes with non-Apple Broadcom Bluetooth controllers such as the BCM20702 in the Dell DW1560, under macOS Tahoe.

It fixes two problems in `bluetoothd`, and both have to be fixed for AirDrop between the Mac and an iPhone to work reliably:

1. BLE advertising never starts at all.
2. Once advertising works, the controller has only one 31-byte advertising slot, and the advertisement the iPhone is waiting for is usually not the one in it.

Since 0.3.0 the patches are no longer tied to one `bluetoothd` build: see [Porting to another macOS build](#porting-to-another-macos-build).

## Tested hardware and software

Original development (0.1.0 - 0.2.1):

- Dell Precision 5540 (SMBIOS `MacBookPro16,1`)
- Dell DW1560 (Wi-Fi BCM4352, Bluetooth `BCM_20702A3`, USB `0489:E07A`)
- macOS Tahoe 26.6 (build `25G72`), Darwin 25.6.0
- Lilu 1.7.2, BrcmPatchRAM3 / BrcmFirmwareStore / BlueToolFixup 2.7.2, AirportBrcmFixup 2.2.0
- Receiving device: iPhone 7 Plus (`iPhone9,2`), iOS 15.8.3

0.3.0:

- Gigabyte Z390 Aorus Pro WiFi, i5-9600KF (SMBIOS `MacPro7,1`), OpenCore 1.0.8
- Fenvi T919 (Wi-Fi BCM4360, Bluetooth `BCM20702A0`, USB `0b05:180a`, ROM firmware v8718, **without** BrcmPatchRAM)
- macOS Tahoe 26.7.2 (build `25G332`) and 26.7.3 (build `25G342`), Darwin 25.6.0
- Lilu 1.7.2, BlueToolFixup 2.7.2, OCLP-CustoMac 3.0.3 root patches ("Modern Wireless")
- iPhone on iOS 27.2, Apple Watch Series 4 on watchOS 10.6.2

## Problem

AirDrop works in only one direction:

| Direction | Without BTLEAdvFix |
|-----------|--------------------|
| iPhone (any iOS) -> Hackintosh | Works |
| Hackintosh -> iPhone on iOS 26 | Works |
| Hackintosh -> another Mac | Works |
| **Hackintosh -> iPhone on iOS 15** | **Fails** |
| **Instant Hotspot from the Hackintosh** | **Fails** (separate cause, see [Known limitations](#known-limitations)) |

When sending to the iOS 15 iPhone:

- the iPhone appears in the AirDrop sheet
- after clicking it, the status stays at **Waiting...** for about 45 seconds and then shows **Failed**
- the iPhone never reacts (no prompt, no notification)
- a real Mac on the same macOS version can AirDrop to the same iPhone without problems

```text
sharingd  [AirDropNW.Discovery] Connectable (Device(... Nm "iPhone", Md iPhone9,2)) requires Bonjour discovery
sharingd  [AirDropNW.Discovery] Bonjour discovery timed out for (Device(... Nm "iPhone", Md iPhone9,2))
sharingd  [AirDrop.XXXXXXXX]    Send StateMachine FAILED {error: Sharing.SFAirDropSend.Failure.resolveFailure(bonjour: false)}
```

The one-way AirDrop issue on DW1560/BCM20702 cards has been reported since macOS Monterey, for example in [acidanthera/bugtracker#1821](https://github.com/acidanthera/bugtracker/issues/1821).

## How AirDrop to an older iPhone works

An iOS 15 iPhone does not keep its AirDrop service published. To send to it, the Mac broadcasts a BLE "AirDrop" advertisement (Apple manufacturer data `4C 00 05 12 ...`). The iPhone sees it and wakes up AWDL (Apple Wireless Direct Link). It then publishes a fresh `_airdrop._tcp` Bonjour service, which the Mac resolves and connects to.

Notes from the iPhone's own logs:

- iOS 15 scans for AirDrop advertisements only while the screen is on (`screen off: No Scan`), and ignores advertisements weaker than −80 dBm.
- Its AirDrop server shuts down after an idle timeout and comes back only when it is woken again.

If the Mac's AirDrop advertisement never reaches the air, the iPhone never wakes up and the send times out.

## Root cause, part 1: advertising never starts

The chain below was traced in the x86_64 slice of `/usr/sbin/bluetoothd` from macOS 26.6 (25G72).

1. **`bluetoothd` selects "multiple advertising" mode.** The `LeBroadcaster` constructor reads a per-platform LE hardware model. That model reports `SupportsMultipleAdvertising = 1` for this platform, without checking whether the real controller supports it:

   ```text
   0x100696a90  cmpb $0x0, 0xa0(%rax)    ; SupportsMultipleAdvertising
   0x100696a97  jne  0x100696ae3         ; skip single-instance setup
   0x100696a99  ...                      ; log "Using a single advertising instance"
   0x100696ad9  movl $0x1, 0x90(%rbx)    ; advertising mode = single instance
   ```

2. **`LeBroadcaster::stackDidStart()` calls `LE_InitMultipleAdvertising`**, which sends a Broadcom vendor-specific (VSC) multi-advertising command.
3. **The BCM20702 rejects that vendor command** (`[Stack.VSC] status STATUS 701`, then `_initMultipleAdvertisingLocalCB`).
4. **The failure is swallowed silently.** The completion handler returns early on a non-zero status without logging. It never runs `movb $0x1, 0x31(%rbx)`, the flag that marks the advertising stack as started.
5. **Every advertisement is then dropped.** On the tested machine, `Stack is not started, updateAdvertisement returning` was logged 689 times in 6 hours, and the advertising state never left `Idle(0) --> Idle(0) stack:OFF`.

In single-instance mode, `stackDidStart()` takes a legacy initialization path that completes with status `0`, and standard Bluetooth 4.0 LE advertising works.

## Root cause, part 2: the AirDrop advertisement loses the only slot

With part 1 fixed, the Mac advertises again, but AirDrop to iOS 15 works only now and then. An HCI trace (see [Debugging](#debugging-with-an-hci-trace)) shows why.

bluetoothd keeps two advertising sessions:

| Session | Content | Raw AD size |
|---------|---------|-------------|
| `CBStackAdvertiserCA` (connectable) | AirPlay Target (`09`), NearbyInfo (`10`), Handoff (`0C`) | about 21 bytes |
| `CBStackAdvertiserNC` (non-connectable) | **AirDrop (`05`)** | 24 bytes |

On a real Mac, each session gets its own advertising set. The BCM20702 has one 31-byte legacy set, and after Flags (3 bytes) and TX Power (3 bytes) only about 25 bytes are left, so the two sessions never fit together.

In single-instance mode, the advertisement builder (`0x1006a1378`) copies each session's raw advertising data into the slot **first-fit, in session-vector order** (copy loops at `0x1006a436e` and `0x1006a4411`). Whichever session comes first wins. NearbyInfo is refreshed every few seconds, so the connectable session is usually first and the AirDrop advertisement is dropped.

Traced payloads sent to the controller (`LE_SetAdvData`), with BTLEAdvFix 0.1.0:

| Requested types (bluetoothd log) | Bytes actually sent to the controller |
|----------------------------------|---------------------------------------|
| `[ 5 9 ]` right after the AirDrop session restarted | `Flags, TxPower, Apple{05}`: AirDrop on air |
| `[ 5 9 16 ]` most of the time | `Flags, TxPower, Apple{09 10}`: **AirDrop dropped** |

This is why AirDrop worked right after toggling Bluetooth or restarting `sharingd`, and then stopped working again within minutes. Changing the BLE address rotation interval does not help.

## How it works

BTLEAdvFix is a Lilu plugin. It routes `_cs_validate_page`, and whenever a code page of `/usr/sbin/bluetoothd` is mapped, it applies the patches that lie on that page.

| # | Location | Change |
|---|----------|--------|
| 1 | LeBroadcaster constructor | `jne` -> `nop nop`: always use a single advertising instance (fixes part 1) |
| 2 | zero padding after `__eh_frame`, end of `__TEXT` | routine that reorders the session vector ([`docs/cave.s`](docs/cave.s)) |
| 3 | advertisement builder | the two 7-byte loads of the session vector begin/end are replaced by a `call` to the routine + 9 `nop`; the routine performs both loads before returning |
| 4 | advertisement builder | the one `je` that jumped to the second replaced load is retargeted to the call. On that path `rax` already holds the vector end, so entering at the call is equivalent, and the routine runs there too |

The addresses differ per macOS build and are listed in the generated tables in [`BTLEAdvFix/builds/`](BTLEAdvFix/builds).

### Advertising order

The routine looks only at each session's raw advertising data, so it does not depend on internal session flags:

1. **NearbyAction first.** A session whose Apple data carries a NearbyAction TLV (type `0F`, length >= 5) goes first. `sharingd` adds it while the Mac is looking for an AirDrop receiver. An iPhone on iOS 27.2 answers the Mac only when it sees this advertisement; with AirDrop always first (0.2.1) sending from the Mac to iOS 27.2 did not work on the tested machine.
2. **Otherwise AirDrop and NearbyInfo take turns**, switching about every 2 seconds (bit 33 of the TSC): either the AirDrop session (`?? FF 4C 00 05`) is first, or the original order is kept, where the connectable session with AirPlay Target and NearbyInfo (`10`) comes first. iOS 27.2 needs NearbyInfo to recognise the Mac before it lists it as an AirDrop target; with AirDrop permanently in the slot the Mac was missing from the iPhone's AirDrop sheet.

The turn is decided only when `bluetoothd` restarts advertising, which happens when a session changes and can be minutes apart; during a send it may not happen at all. That is fine where rule 1 covers sending, but on **macOS 26.6 (`25G72`)** `sharingd` adds no NearbyAction while sending AirDrop (only for NeedsKeyboard and, very briefly, NeedsCLink). There a whole send can fall into the NearbyInfo turn: an iPhone on iOS 15 never sees the AirDrop advertisement, never publishes its AirDrop service, and the send fails with "Bonjour discovery timed out". So the builds in `AIRDROP_FIRST_BUILDS` (`tools/port.py`, currently `25G72`) use a different rule:

- **AirDrop first**, as in 0.2.1;
- **except while another session is boosted**: when a session asks for a 30 ms interval (`0x30` or less) and the AirDrop session does not, that session goes first. `sharingd` boosts the connectable session for about 5 seconds when Apple Pay looks for the iPhone (NearbyInfo, `ApplePayLegacy`), for Handoff and when its data changes. Each boost start and end restarts advertising, so the order is chosen again at both edges. Without this, Apple Pay through the iPhone timed out whenever AirDrop was advertised, which on 26.6 is for minutes after AirDrop is opened.

The interval is the session's requested advertising interval (`uint16` at `session + 0xde` on 25G72). `tools/port.py` finds the offset from the builder code that takes the minimum interval over all sessions. The other builds keep the order above.

### Safety properties

- The kext contains no pattern search. It picks a table by the running macOS build (`kern.osversion`); on a build without a table nothing is patched.
- When the Mach-O header page of `bluetoothd` is mapped, its `LC_UUID` is compared with the one the table was generated from. If it differs, patching stops until the next boot.
- A patch is written only at its exact file offset and only over its exact original bytes.
- There are no one-shot flags. Patches 2, 3 and 4 depend on each other, so every page-in of `bluetoothd` is patched again.
- The System volume and `bluetoothd` on disk are not modified.
- The plugin only loads on macOS Tahoe (Darwin 25).

## Result

HCI trace and logs with BTLEAdvFix 0.2.1 on the tested machine. The two sends were 11 minutes after boot, the window in which 0.1.0 used to fail.

| | Send 1 | Send 2 |
|---|---|---|
| bluetoothd starts the AirDrop advertisement | 02:40:55.630 | 02:48:18.272 |
| controller receives `Flags, TxPower, Apple{05}` | 02:40:55.638 | 02:48:18.280 |
| iPhone appears on AWDL with a **fresh** `_airdrop._tcp` name | 02:40:56.904 | 02:48:18.808 |
| send result | **completed** in 1.4 s | **completed** in 1.8 s |
| AirDrop advertisement stops, slot goes back to AirPlay/NearbyInfo | 02:41:10 | 02:48:24 |

The fresh Bonjour service name about one second after the advertisement shows that the Mac's advertisement is what woke the iPhone.

## Installation

1. Download `BTLEAdvFix-<version>-RELEASE.zip` from the latest release and extract `BTLEAdvFix.kext`.
2. Copy it to `EFI/OC/Kexts`, replacing any older version. Delete the old folder first; a Finder merge can leave the old binary in place.
3. Add it to `Kernel -> Add` after `Lilu.kext`. Keep your existing Bluetooth kexts.
4. Reboot.

```bash
rm -rf /Volumes/EFI/EFI/OC/Kexts/BTLEAdvFix.kext
ditto BTLEAdvFix.kext /Volumes/EFI/EFI/OC/Kexts/BTLEAdvFix.kext
plutil -extract CFBundleVersion raw /Volumes/EFI/EFI/OC/Kexts/BTLEAdvFix.kext/Contents/Info.plist
```

Recommended load order:

```text
Lilu.kext
BlueToolFixup.kext
BrcmFirmwareData.kext / BrcmFirmwareStore.kext
BrcmPatchRAM3.kext
BTLEAdvFix.kext
```

BTLEAdvFix does not replace BrcmPatchRAM or BlueToolFixup. You still need them for basic Bluetooth.

Boot arguments:

| Boot argument | Effect |
|---------------|--------|
| `-bladvoff` | Disable the plugin |
| `-bladvdbg` | Enable debug logging (DEBUG build of the plugin and Lilu) |
| `-bladvbeta` | Allow loading on unsupported macOS versions |

## Verification

Use `/usr/bin/log` with the full path. In zsh, plain `log` is a shell builtin and prints nothing useful.

```bash
kmutil showloaded | grep -iE 'Lilu|BTLEAdvFix'
```

```bash
/usr/bin/log show --last boot --info --style compact \
  --predicate 'process == "bluetoothd" AND (eventMessage CONTAINS "single advertising" OR eventMessage CONTAINS "Stack is not started" OR eventMessage CONTAINS "Advertising instance Apple Types")' \
  | tail -20
```

What to look for:

- **Working:** `Using a single advertising instance`, then lines such as `[0] Starting Connectable Advertising instance Apple Types [ 9 16 ]`.
- **Part 1 not patched:** repeated `Stack is not started, updateAdvertisement returning`.
- **Something wrong with patches 2-4:** no `Apple Types` lines at all. Switch back to 0.1.0 or use `-bladvoff`, and open an issue with an HCI trace.

Then send a file with AirDrop to an older iPhone with its screen on.

## Debugging with an HCI trace

The `Apple Types` list in the bluetoothd log is what was *requested*, not what the controller transmits. To see the real bytes, enable bluetoothd's built-in PacketLogger file tracing:

```bash
sudo mkdir -p /Library/Logs/Bluetooth
sudo defaults write /Library/Preferences/com.apple.MobileBluetooth.debug HCITraces -dict HCIFileTraces -bool true
notifyutil -p com.apple.bluetooth.prefsChanged
```

Toggling Bluetooth does not reload this setting; the notification (or restarting `bluetoothd`) does. The trace is written to `/var/root/Library/Logs/Bluetooth/bluetoothd-hci-latest.pklg`:

```bash
sudo cp /var/root/Library/Logs/Bluetooth/bluetoothd-hci-latest.pklg ~/Desktop/
sudo chown $USER ~/Desktop/bluetoothd-hci-latest.pklg
python3 tools/pklg_adv.py ~/Desktop/bluetoothd-hci-latest.pklg --utc-offset 0
```

`tools/pklg_adv.py` prints every advertising payload sent to the controller, decoded into AD structures and Apple continuity types (`05` AirDrop, `09` AirPlay Target, `0c` Handoff, `10` NearbyInfo, ...), plus `LE_CreateConn` commands and failed HCI commands. The timestamps in these traces are already local time, so pass `--utc-offset 0`.

Turn tracing off when done:

```bash
sudo defaults delete /Library/Preferences/com.apple.MobileBluetooth.debug HCITraces
```

## Porting to another macOS build

Nothing in the kext source is tied to a build. The build-specific bytes live in generated tables, one per macOS build, in `BTLEAdvFix/builds/`. To add a build, take a **clean** copy of its `/usr/sbin/bluetoothd` and run:

```bash
python3 tools/port.py /path/to/bluetoothd 25G332
```

then rebuild the kext. While the kext is active, reads of `/usr/sbin/bluetoothd` can return patched pages, so make the copy after booting with `-bladvoff` (or on a build the kext does not know, where it is inactive anyway).

`tools/port.py`:

- finds the three patch sites by signature. Bytes that change between builds (branch distances, stack offsets) are wildcards; the stack offsets of the session vector are read from the replaced instructions and passed to [`docs/cave.s`](docs/cave.s);
- places the routine in the zero padding after `__eh_frame`, at `__eh_frame + 0x60` (`+0x180` and up belongs to BTLERPAConnect);
- refuses to write anything unless every signature matches exactly once, the routine area is free, the retargeted `je` really pointed into the replaced instructions, and no other branch in `__text` lands inside replaced bytes. That last mistake broke an earlier 0.2.0 build: Apple data disappeared from every advertisement;
- writes `builds/<build>.inc` with the slice offset, the `LC_UUID` and, for each patch, its file offset, original bytes and replacement bytes.

`python3 tools/port.py --check <bluetoothd> <build>` regenerates a table in memory and compares it with the committed one.

If a signature no longer matches, the code changed and has to be looked at again:

1. `otool -arch x86_64 -tV bluetoothd > bluetoothd.asm`
2. Patch 1: find the string `Using a single advertising instance`. A few instructions above it is the `cmpb $0x0, 0xa0(%rax)` / `jne` pair to `nop` out.
3. Patches 2-4: in the advertisement builder, find the two loops that walk the session vector (two `movq ...(%rbp)` loads before a loop calling the raw-data size/data accessors). Update the signatures in `tools/port.py` and, if the vector or session layout changed, [`docs/cave.s`](docs/cave.s).

## Building

Dependencies:

- Xcode
- [Lilu](https://github.com/acidanthera/Lilu) DEBUG build (contains the plugin SDK)
- [MacKernelSDK](https://github.com/acidanthera/MacKernelSDK)

Place them in the repository root:

```text
Lilu.kext
MacKernelSDK
```

```bash
xcodebuild \
  -project BTLEAdvFix.xcodeproj \
  -target BTLEAdvFix \
  -configuration Release \
  SYMROOT="$PWD/build" \
  build
```

The kext and a release zip are written to `build/Release`.

The code that patches a page has no kernel dependencies (`BTLEAdvFix/page_patcher.hpp`), so it can be run against a real binary before booting with it:

```bash
clang++ -std=c++14 -I BTLEAdvFix tools/test_apply.cpp -o /tmp/test_apply
/tmp/test_apply <clean bluetoothd> 25G332 /tmp/bluetoothd.patched
```

`tools/test_order.py` runs the AirDrop-first routine (see [Advertising order](#advertising-order)) on fake session lists and checks the resulting order. It executes the assembled routine, so it needs an x86_64 Mac.

## Compatibility

Verified on:

| macOS | Build | Bluetooth | Receiver | Version |
|-------|-------|-----------|----------|---------|
| Tahoe 26.6 | `25G72` | Dell DW1560 / BCM20702A3 | iPhone 7 Plus, iOS 15.8.3 | 0.2.1; 0.3.2 (AirDrop); 0.3.3 pending, see below |
| Tahoe 26.7.2 | `25G332` | Fenvi T919 / BCM20702A0 (USB `0b05:180a`, firmware v8718, no BrcmPatchRAM) | iPhone on iOS 27.2 | 0.3.0 |
| Tahoe 26.7.3 | `25G342` | same machine | iPhone on iOS 27.2 | 0.3.0 |

The `25G72` table was added in 0.3.1. It was generated from a copy of that build's `bluetoothd` whose 2885 code pages all match the page hashes in Apple's code signature, and `tools/test_apply.cpp` applies all 4 patches together with BTLERPAConnect 0.3.1 in either order. The patch sites are the same as in 0.2.1. With the 0.3.0 order (0.3.1), sending from the Mac to the iOS 15.8.3 iPhone failed with "Bonjour discovery timed out"; since 0.3.2 this build uses the AirDrop-first order that 0.2.1 used on this machine, and 0.3.3 lets boosted sessions (Apple Pay, Handoff) through.

Feature matrix on that machine, same on 26.7.2 and 26.7.3 (BTLEAdvFix 0.3.0 + BTLERPAConnect 0.3.0, OCLP-CustoMac 3.0.3 "Modern Wireless" for Wi-Fi/AWDL):

| Feature | Result |
|---------|--------|
| AirDrop Mac -> iPhone (iOS 27.2) | works |
| AirDrop iPhone (iOS 27.2) -> Mac | works |
| Handoff, AirPlay | work |
| Instant Hotspot | works (needs BTLERPAConnect) |
| Auto Unlock with Apple Watch | works (needs BTLERPAConnect) |
| bluetoothd restarts | none |

Other BCM20702-based cards (DW1830, BCM94352Z) are likely affected by the same issues, but have not been tested. Cards with a native Apple Bluetooth module (for example BCM94360NG/CS2) do not need this plugin.

## Known limitations

- **Instant Hotspot needs [BTLERPAConnect](https://github.com/trungkiengrxer/BTLERPAConnect).** BTLEAdvFix only makes the Mac advertise; the Bluetooth 4.0 limits that break LE connections opened by the Mac are handled there.
- **One advertisement at a time.** The controller has a single 31-byte legacy advertising set. While the Mac looks for an AirDrop receiver the NearbyAction advertisement holds the slot; otherwise AirDrop and AirPlay/NearbyInfo/Handoff take turns about every 2 seconds, so discovery by other devices can take a few seconds longer than on a real Mac.
- **The order is chosen per macOS build, not per iPhone.** On `25G72` AirDrop is first except during boosts, which suits iOS 15; per the iOS 27.2 tests above this can hide the Mac from the AirDrop sheet of an iPhone on iOS 27.2 (not tested on 26.6). A long run of boosts (for example repeated Handoff updates) keeps AirDrop out of the slot for that time.
- **Other Continuity features are not fixed.** Other Apple vendor commands are still rejected by the BCM20702, for example `BD_VSC_LE_META_*` with status `701`.

## Safety warning

This project patches executable code of a system daemon at runtime, including inserting a small routine.

An unsupported macOS build is ignored by design: the kext has no table for it and does not search for patterns at runtime. Even so, any patch to `bluetoothd` can cause Bluetooth instability.

Keep a working EFI backup, and keep a reliable way to disable the plugin: the `-bladvoff` boot argument, removing the kext, or going back to 0.1.0.

## Changelog

- **0.3.3**: on macOS 26.6 (`25G72`) a session that asks for a 30 ms interval goes before AirDrop while it does, so Apple Pay through the iPhone and Handoff work while AirDrop is advertised (Apple Pay timed out with 0.3.2). New `tools/test_order.py`.
- **0.3.2**: on macOS 26.6 (`25G72`) the AirDrop advertisement is always first again, as in 0.2.1: that build adds no NearbyAction while sending, and with the 0.3.0 order Mac -> iOS 15.8.3 failed. The order is chosen per build in `tools/port.py` (`AIRDROP_FIRST_BUILDS`); the 26.7.x tables are unchanged.
- **0.3.1**: table for macOS 26.6 (25G72). The comment on the shared routine layout now reserves `__eh_frame + 0x180 .. 0x400` for BTLERPAConnect 0.3.1. Releases are built by GitHub Actions.
- **0.3.0**: build-independent sources. Patch sites are found by signature with `tools/port.py`, which generates one table per macOS build (`BTLEAdvFix/builds/`); the kext picks the table by macOS build, checks the `LC_UUID` of `bluetoothd` and never patches an unknown build. New advertising order for iOS 27.2 (NearbyAction first, otherwise AirDrop and NearbyInfo take turns). Added macOS 26.7.2 (25G332) and 26.7.3 (25G342). `tools/check_patterns.py` is replaced by `tools/port.py --check`.
- **0.2.1**: AirDrop-first routine (patches 2-4); AirDrop to iOS 15 now works reliably. `tools/check_patterns.py` checks branch targets; `tools/pklg_adv.py` decodes HCI traces.
- **0.2.0** (not released): first AirDrop-first build. A branch into replaced bytes broke advertisement data.
- **0.1.0**: single advertising instance (patch 1). BLE advertising works, but AirDrop to iOS 15 works only intermittently.

## Credits

- Acidanthera for Lilu, MacKernelSDK, BrcmPatchRAM and BlueToolFixup
- Bosca27 for the port to macOS 26.7.2, the iOS 27.2 investigation (logs and HCI traces from Mac and iPhone) and all testing on the Fenvi T919; the 0.3.0 analysis, routine and tooling were written with Claude Code (Anthropic)
- KGP / Klaus (OCLP-CustoMac) for the request to make the patching independent of the macOS build
- FeatureUnlock and RealtekUVCFix as reference implementations for runtime page patching
- Everyone who reported and investigated the one-way AirDrop issue in [acidanthera/bugtracker#1821](https://github.com/acidanthera/bugtracker/issues/1821)

## License

See `LICENSE`.

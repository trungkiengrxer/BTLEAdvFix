# BTLEAdvFix

Experimental Lilu plugin that restores Bluetooth LE advertising on Hackintoshes with non-Apple Broadcom Bluetooth controllers such as the BCM20702 in the Dell DW1560, under macOS Tahoe.

It fixes two problems in `bluetoothd`, and both have to be fixed for **AirDrop from the Mac to older iPhones (iOS 15)** to work reliably:

1. BLE advertising never starts at all.
2. Once advertising works, the AirDrop advertisement is usually dropped in favour of other Continuity advertisements, because the controller has only one 31-byte advertising slot.

## Tested hardware and software

- Dell Precision 5540 (SMBIOS `MacBookPro16,1`)
- Dell DW1560 (Wi-Fi BCM4352, Bluetooth `BCM_20702A3`, USB `0489:E07A`)
- macOS Tahoe 26.6 (build `25G72`)
- Darwin 25.6.0
- Intel x86_64 Hackintosh
- Lilu 1.7.2
- BrcmPatchRAM3 / BrcmFirmwareStore / BlueToolFixup 2.7.2
- AirportBrcmFixup 2.2.0
- Receiving device: iPhone 7 Plus (`iPhone9,2`), iOS 15.8.3

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

BTLEAdvFix is a Lilu plugin. It routes `_cs_validate_page`, and whenever a code page of `/usr/sbin/bluetoothd` is mapped, it applies four verified byte patches to that page.

| # | Location | Change |
|---|----------|--------|
| 1 | `0x100696a97` (LeBroadcaster constructor) | `jne` -> `nop nop`: always use a single advertising instance (fixes part 1) |
| 2 | `0x100aa23b0` (zero padding after `__eh_frame`, end of `__TEXT`) | 116-byte routine that moves the AirDrop session to the front of the session vector ([`docs/cave.s`](docs/cave.s)) |
| 3 | `0x1006a4351` (advertisement builder) | the two 7-byte loads of the session vector begin/end are replaced by `call 0x100aa23b0` + 9 `nop`; the routine performs both loads before returning |
| 4 | `0x1006a3ce3` (advertisement builder) | `je 0x1006a4358` -> `je 0x1006a4351`. This branch jumped into the second replaced load. On that path `rax` already holds the vector end, so entering at the call is equivalent, and the routine runs there too |

The routine identifies the AirDrop session by its raw data, `?? FF 4C 00 05` (an Apple manufacturer AD whose first type is AirDrop), so it does not depend on internal session flags. When no AirDrop advertisement is active, the order is unchanged and everything behaves exactly as with patch 1 alone.

The result matches what a real Mac does with separate advertising sets: while AirDrop is active, the slot carries the AirDrop advertisement; otherwise it carries AirPlay, NearbyInfo and Handoff as before.

Safety properties:

- Every pattern occurs exactly once in `bluetoothd` 25G72. If a pattern is not found, nothing is patched.
- There are no one-shot flags. Patches 2, 3 and 4 depend on each other, so every page-in of `bluetoothd` is patched again, and pages that are already patched no longer match.
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

The patches are tied to the exact bytes of `bluetoothd` in macOS 26.6 (25G72). After a macOS update, run:

```bash
python3 tools/check_patterns.py
```

It checks that:

- every pattern matches exactly once;
- the inserted call lands on the routine and the retargeted `je` lands on the call;
- no branch anywhere in `__text` lands inside replaced bytes. That exact mistake broke an earlier 0.2.0 build: Apple data disappeared from every advertisement.

If it fails, new patterns are needed:

1. `otool -arch x86_64 -tV /usr/sbin/bluetoothd > bluetoothd.asm`
2. Patch 1: find the string `Using a single advertising instance`. A few instructions above it is the `cmpb $0x0, 0xa0(%rax)` / `jne` pair to `nop` out.
3. Patches 2-4: in the advertisement builder, find the two loops that walk the session vector (`movq -0x6db8(%rbp)` / `movq -0x6dc0(%rbp)` before a loop calling the raw-data size/data accessors). Re-derive the vector and session-data offsets, rebuild [`docs/cave.s`](docs/cave.s), and re-check every branch into the replaced instructions.

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

The kext and a release zip are written to `build/Release`. To rebuild the routine bytes:

```bash
clang -c -arch x86_64 docs/cave.s -o /tmp/cave.o && otool -t /tmp/cave.o
```

## Compatibility

Currently verified only on:

- Dell DW1560 / BCM20702A3 Bluetooth
- macOS Tahoe 26.6 (25G72)
- Intel x86_64
- AirDrop receiver: iPhone on iOS 15.8.3

Other BCM20702-based cards (DW1830, BCM94352Z, Fenvi with BCM20702) are likely affected by the same issues, but have not been tested. Cards with a native Apple Bluetooth module (for example BCM94360NG/CS2) do not need this plugin.

## Known limitations

- **Instant Hotspot still fails.** This is a different problem. The Mac must open a BLE connection to the iPhone. bluetoothd adds the iPhone's *identity* address (`Public 98:10:...`) to the controller's filter accept list and expects the controller to resolve the iPhone's random private address (RPA). That needs Bluetooth 4.2 LL Privacy, or the Broadcom `ADD_IRK_TO_LIST` vendor command that Apple's own firmware implements. The DW1560 firmware has neither (the vendor command returns `701`), so `LE_CreateConn` never matches the iPhone. The first attempt is also rejected with `0x0C Command Disallowed` because the controller is scanning. Connections initiated by the iPhone do work (bluetoothd resolves the RPA in software), but Instant Hotspot needs the Mac to connect. Workaround: turn on Personal Hotspot on the iPhone and join it from the Mac's Wi-Fi menu.
- **One advertisement at a time.** While AirDrop is active, AirPlay/NearbyInfo/Handoff advertisements are paused. This is a hardware limit of a single legacy advertising set.
- **Other Continuity features are not fixed.** Other Apple vendor commands are still rejected by the BCM20702, for example `BD_VSC_LE_META_*` with status `701`.

## Safety warning

This project patches executable code of a system daemon at runtime, including inserting a small routine.

A mismatched macOS build is ignored by design: patterns simply do not match. Even so, any patch to `bluetoothd` can cause Bluetooth instability.

Keep a working EFI backup, and keep a reliable way to disable the plugin: the `-bladvoff` boot argument, removing the kext, or going back to 0.1.0.

## Changelog

- **0.2.1**: AirDrop-first routine (patches 2-4); AirDrop to iOS 15 now works reliably. `tools/check_patterns.py` checks branch targets; `tools/pklg_adv.py` decodes HCI traces.
- **0.2.0** (not released): first AirDrop-first build. A branch into replaced bytes broke advertisement data.
- **0.1.0**: single advertising instance (patch 1). BLE advertising works, but AirDrop to iOS 15 works only intermittently.

## Credits

- Acidanthera for Lilu, MacKernelSDK, BrcmPatchRAM and BlueToolFixup
- FeatureUnlock and RealtekUVCFix as reference implementations for runtime page patching
- Everyone who reported and investigated the one-way AirDrop issue in [acidanthera/bugtracker#1821](https://github.com/acidanthera/bugtracker/issues/1821)

## License

See `LICENSE`.

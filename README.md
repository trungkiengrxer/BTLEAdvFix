# BTLEAdvFix

Experimental Lilu plugin that restores Bluetooth LE advertising on Hackintoshes with non-Apple Broadcom Bluetooth controllers such as the BCM20702 in the Dell DW1560, under macOS Tahoe.

With BLE advertising working again, the Mac can wake nearby devices, so **AirDrop from the Mac to older iPhones (iOS 15)** works again.

## Tested hardware and software

- Dell Precision 5540 (SMBIOS `MacBookPro16,1`)
- Dell DW1560 (Wi-Fi BCM4352, Bluetooth `BCM_20702A3`, USB `0489:E07A`)
- macOS Tahoe 26.6 (build `25G72`)
- Darwin 25.6.0
- Intel x86_64 Hackintosh
- Lilu 1.7.2
- BrcmPatchRAM3 / BrcmFirmwareStore / BlueToolFixup 2.7.2
- AirportBrcmFixup 2.2.0

## Problem

AirDrop works in only one direction:

| Direction | Before |
|-----------|--------|
| iPhone (any iOS) -> Hackintosh | Works |
| Hackintosh -> iPhone on iOS 26 | Works |
| Hackintosh -> another Mac | Works |
| **Hackintosh -> iPhone on iOS 15** | **Fails** |
| **Instant Hotspot from the Hackintosh** | **Fails** |

When sending to the iOS 15 iPhone:

- the iPhone appears in the AirDrop sheet
- after clicking it, the status stays at **Waiting...** for about 45 seconds and then shows **Failed**
- the iPhone never reacts (no prompt, no notification)
- a real Mac on the same macOS version can AirDrop to the same iPhone without problems

This has been reported for DW1560/BCM20702 cards since macOS Monterey, for example in [acidanthera/bugtracker#1821](https://github.com/acidanthera/bugtracker/issues/1821).

## Root cause

The Mac never sends any Bluetooth LE advertisement. It can scan (that is why it still sees iPhones and can *receive* AirDrop), but it cannot advertise.

Sending AirDrop to an older iPhone requires the Mac to broadcast a BLE "AirDrop" advertisement (Apple manufacturer data `4C 00 05 12 ...`). That advertisement wakes the iPhone, which then brings up AWDL and publishes its `_airdrop._tcp` Bonjour service. Without it, the Mac waits for Bonjour, times out and fails:

```text
sharingd  [AirDropNW.Discovery] Bonjour discovery timed out for (<private>)
sharingd  [AirDrop.XXXXXXXX]    Send StateMachine FAILED {error: Error Domain=Sharing.SFAirDropSend.Failure Code=1}
```

Instant Hotspot fails for the same reason.

### Why advertising never starts

The chain below was traced in the x86_64 slice of `/usr/sbin/bluetoothd` from macOS 26.6 (25G72).

1. **`bluetoothd` selects "multiple advertising" mode.** The `LeBroadcaster` constructor reads a per-platform LE hardware model. That model reports `SupportsMultipleAdvertising = 1` for this platform. It does not check whether the real controller supports it. bluetoothd therefore skips the `Using a single advertising instance` branch and keeps advertising mode `0`:

   ```text
   0x100696a90  cmpb $0x0, 0xa0(%rax)    ; SupportsMultipleAdvertising
   0x100696a97  jne  0x100696ae3         ; skip single-instance setup
   0x100696a99  ...                      ; log "Using a single advertising instance"
   0x100696ad9  movl $0x1, 0x90(%rbx)    ; advertising mode = single instance
   ```

2. **`LeBroadcaster::stackDidStart()` calls `LE_InitMultipleAdvertising`.** In multi-advertising mode the stack sends a Broadcom vendor-specific (VSC) multi-advertising command.

3. **The BCM20702 rejects that vendor command.** The BCM20702 dates from 2010 and does not implement it:

   ```text
   bluetoothd  [Stack.VSC] status  STATUS 701
   bluetoothd  [Stack.LE]   _initMultipleAdvertisingLocalCB
   ```

4. **The failure is swallowed silently.** The completion callback forwards status `701` to `LeBroadcaster`. That handler returns early on a non-zero status, without logging. It therefore never runs `movb $0x1, 0x31(%rbx)`, the flag that marks the advertising stack as started.

5. **Every advertisement is then dropped.** `LeBroadcaster::updateAdvertisement` checks that flag first:

   ```text
   bluetoothd  [Server.LE.Advertise] Advertising state change:  Idle(0) --> Idle(0) stack:OFF
   bluetoothd  [Server.LE.Advertise] Stack is not ready, waiting for the stack to start
   bluetoothd  [Server.LE.Advertise] Stack is not started, updateAdvertisement returning
   ```

   On the tested machine this was logged 689 times in 6 hours, and the advertising state never left `Idle ... stack:OFF`.

If the advertising mode is `1` (single instance), `stackDidStart()` uses the legacy initialization path instead. That path completes with status `0`, so the stack flag is set and standard Bluetooth 4.0 LE advertising works.

## How it works

BTLEAdvFix is a Lilu plugin.

It routes `_cs_validate_page`. When a code page of `/usr/sbin/bluetoothd` is mapped, it looks for one verified 12-byte pattern in the `LeBroadcaster` constructor and replaces the `jne` with two `nop`s:

```text
Original:     80 B8 A0 00 00 00 00 75 4A 48 8D 05
Replacement:  80 B8 A0 00 00 00 00 90 90 48 8D 05
```

As a result, bluetoothd always takes the "single advertising instance" branch. It never sends the unsupported multi-advertising command, and the advertising stack starts normally.

- The pattern occurs exactly once in the whole `bluetoothd` file (both slices) of build 25G72.
- If the pattern is not found, nothing is patched.
- The System volume and `bluetoothd` on disk are not modified.
- The plugin only loads on macOS Tahoe (Darwin 25).

## Result

After installing BTLEAdvFix on the tested machine:

```text
bluetoothd  [Server.LE.Advertise] Using a single advertising instance
bluetoothd  [Server.LE.Advertise] Advertising state change:  Idle(0) --> Starting(1) stack:ON
bluetoothd  [Server.LE.Advertise] Advertising state change:  Starting(1) --> Advertising(2) stack:ON
bluetoothd  [CBAdvertiserDaemon]  AirDrop advertise WP start: <...>, rate 30.00 ms
sharingd    [AirDrop.XXXXXXXX]    Send StateMachine UPLOAD END
sharingd    [AirDrop.XXXXXXXX]    Send StateMachine END
```

- `Stack is not started` no longer appears.
- AirDrop from the Hackintosh to the iOS 15 iPhone completes in about 2 seconds and the file is received.

## Installation

1. Download `BTLEAdvFix-<version>-RELEASE.zip` from the latest release and extract `BTLEAdvFix.kext`.
2. Copy it to `EFI/OC/Kexts`.
3. Add it to `Kernel -> Add` after `Lilu.kext`. Keep your existing Bluetooth kexts.
4. Reboot.

Recommended load order:

```text
Lilu.kext
BlueToolFixup.kext
BrcmFirmwareData.kext / BrcmFirmwareStore.kext
BrcmPatchRAM3.kext
BTLEAdvFix.kext
```

BTLEAdvFix does not replace BrcmPatchRAM or BlueToolFixup. You still need them to get basic Bluetooth working on the controller.

Boot arguments:

| Boot argument | Effect |
|---------------|--------|
| `-bladvoff` | Disable the plugin |
| `-bladvdbg` | Enable debug logging (DEBUG build of the plugin and Lilu) |
| `-bladvbeta` | Allow loading on unsupported macOS versions |

## Verification

Use `/usr/bin/log` with the full path. In zsh, plain `log` is a shell builtin and prints nothing useful.

Check that the kext is loaded:

```bash
kmutil showloaded | grep -iE 'Lilu|BTLEAdvFix'
```

Check that bluetoothd now uses a single advertising instance and the stack is `ON`:

```bash
/usr/bin/log show --last boot --style compact \
  --predicate 'process == "bluetoothd" AND (eventMessage CONTAINS "single advertising" OR eventMessage CONTAINS "Stack is not started" OR eventMessage CONTAINS "Advertising state change")' \
  | tail -20
```

What to look for:

- **Patched:** `Using a single advertising instance`, then transitions such as `Starting(1) --> Advertising(2) stack:ON`.
- **Not patched:** `Idle(0) --> Idle(0) stack:OFF`, followed by repeated `Stack is not started, updateAdvertisement returning`.

Then send a file with AirDrop from the Mac to an older iPhone.

## Porting to another macOS build

The patch is tied to the exact bytes of `bluetoothd` in macOS 26.6 (25G72). After a macOS update, check whether the pattern still exists:

```bash
python3 -c 'd=open("/usr/sbin/bluetoothd","rb").read(); print(d.count(bytes.fromhex("80b8a000000000754a488d05")))'
```

If this prints `1`, the current release still applies. If it prints `0`, a new pattern is needed. To find it:

1. Disassemble the x86_64 slice:

   ```bash
   otool -arch x86_64 -tV /usr/sbin/bluetoothd > bluetoothd.asm
   ```

2. Find the reference to the string `Using a single advertising instance`.
3. A few instructions above it, find the `cmpb $0x0, 0xa0(%rax)` / `jne` pair that skips that branch.
4. Add an original/replacement pair to `BTLEAdvFix/kern_start.cpp` that turns that `jne` into `nop`s.

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

Build the Release configuration:

```bash
xcodebuild \
  -project BTLEAdvFix.xcodeproj \
  -target BTLEAdvFix \
  -configuration Release \
  SYMROOT="$PWD/build" \
  build
```

The kext and a release zip are written to `build/Release`.

## Compatibility

Currently verified only on:

- Dell DW1560 / BCM20702A3 Bluetooth
- macOS Tahoe 26.6 (25G72)
- Intel x86_64

Other BCM20702-based cards (DW1830, BCM94352Z, Fenvi with BCM20702) are likely affected by the same issue, but have not been tested. Cards with a native Apple Bluetooth module (for example BCM94360NG/CS2) do not need this plugin.

## Known limitations

- **Single advertising instance only.** bluetoothd now rotates all of its advertisements (AirDrop, Handoff, Nearby, and others) through one advertising set, as Macs with older controllers do. Features that rely on several advertisements at the same time may behave differently.
- **Instant Hotspot not yet confirmed.** It depends on the same BLE advertising and is expected to improve, but has not been verified yet.
- **Other Continuity features are not fixed.** Many other Apple-specific vendor commands are still rejected by the BCM20702, for example `BD_VSC_LE_META_*` with status `701`. Features that depend on them may still fail.

## Safety warning

This project patches executable code of a system daemon at runtime.

A mismatched macOS build or a changed byte pattern is ignored by design. Even so, any patch to `bluetoothd` can cause Bluetooth instability.

Keep a working EFI backup, and keep a reliable way to disable the plugin: the `-bladvoff` boot argument, or removing the kext.

## Credits

- Acidanthera for Lilu, MacKernelSDK, BrcmPatchRAM and BlueToolFixup
- FeatureUnlock and RealtekUVCFix as reference implementations for runtime page patching
- Everyone who reported and investigated the one-way AirDrop issue in [acidanthera/bugtracker#1821](https://github.com/acidanthera/bugtracker/issues/1821)

## License

See `LICENSE`.

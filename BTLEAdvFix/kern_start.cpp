//
//  kern_start.cpp
//  BTLEAdvFix.kext
//
//  Forces bluetoothd to use a single LE advertising instance.
//
//  Why: on non-Apple Broadcom controllers (BCM20702, e.g. DW1560) bluetoothd
//  picks "multiple advertising" mode, sends a Broadcom vendor command the chip
//  rejects (STATUS 701), and LeBroadcaster never sets its stack-started flag.
//  Every advertisement is then dropped ("Stack is not started,
//  updateAdvertisement returning"), so the Mac cannot wake peers for AirDrop
//  (iOS 15) or Instant Hotspot.
//
//  macOS Tahoe 26.6 (25G72), /usr/sbin/bluetoothd x86_64, LeBroadcaster ctor:
//
//    0x100696a90  cmpb $0x0, 0xa0(%rax)   ; SupportsMultipleAdvertising
//    0x100696a97  jne  0x100696ae3        ; skip "Using a single advertising instance"
//    0x100696a99  leaq ...(%rip), %rax
//
//  NOP the jne so the single-instance branch always runs (sets mode 1),
//  which makes stackDidStart take the legacy init path that completes with
//  status 0.
//

#include <Headers/plugin_start.hpp>
#include <Headers/kern_api.hpp>
#include <Headers/kern_user.hpp>

#include <sys/vnode.h>

#define MODULE_SHORT "bladv"

static const char bluetoothdPath[] = "/usr/sbin/bluetoothd";

static mach_vm_address_t originalCsValidatePage {};
static bool patchApplied {};

// ponytail: one exact pattern for 25G72; add a pair here when another build differs.
static const uint8_t singleAdvOriginal[] {
    0x80, 0xB8, 0xA0, 0x00, 0x00, 0x00, 0x00,   // cmpb $0x0, 0xa0(%rax)
    0x75, 0x4A,                                 // jne  +0x4a
    0x48, 0x8D, 0x05                            // leaq ...(%rip), %rax
};

static const uint8_t singleAdvReplacement[] {
    0x80, 0xB8, 0xA0, 0x00, 0x00, 0x00, 0x00,
    0x90, 0x90,
    0x48, 0x8D, 0x05
};

static_assert(
    sizeof(singleAdvOriginal) == sizeof(singleAdvReplacement),
    "patch size mismatch"
);

static void patchedCsValidatePage(
    vnode_t vnode,
    memory_object_t pager,
    memory_object_offset_t pageOffset,
    const void *data,
    int *validated,
    int *tainted,
    int *nx
) {
    FunctionCast(
        patchedCsValidatePage,
        originalCsValidatePage
    )(vnode, pager, pageOffset, data, validated, tainted, nx);

    if (patchApplied || data == nullptr)
        return;

    char path[PATH_MAX] {};
    int pathLength = sizeof(path);

    if (vn_getpath(vnode, path, &pathLength) != 0)
        return;

    if (strcmp(path, bluetoothdPath) != 0)
        return;

    bool replaced = KernelPatcher::findAndReplace(
        const_cast<void *>(data),
        PAGE_SIZE,
        singleAdvOriginal,
        sizeof(singleAdvOriginal),
        singleAdvReplacement,
        sizeof(singleAdvReplacement)
    );

    if (!replaced)
        return;

    patchApplied = true;

    SYSLOG(
        MODULE_SHORT,
        "forced single LE advertising instance in %s at page offset 0x%llx",
        path,
        static_cast<unsigned long long>(pageOffset)
    );
}

static void pluginStart() {
    DBGLOG(MODULE_SHORT, "BTLEAdvFix starting");

    lilu.onPatcherLoadForce([](void *, KernelPatcher &patcher) {
        KernelPatcher::RouteRequest request(
            "_cs_validate_page",
            patchedCsValidatePage,
            originalCsValidatePage
        );

        if (!patcher.routeMultipleLong(KernelPatcher::KernelID, &request, 1))
            SYSLOG(MODULE_SHORT, "failed to route _cs_validate_page");
    });
}

static const char *bootargOff[] {
    "-bladvoff"
};

static const char *bootargDebug[] {
    "-bladvdbg"
};

static const char *bootargBeta[] {
    "-bladvbeta"
};

PluginConfiguration ADDPR(config) {
    xStringify(PRODUCT_NAME),
    parseModuleVersion(xStringify(MODULE_VERSION)),
    LiluAPI::AllowNormal,
    bootargOff,
    arrsize(bootargOff),
    bootargDebug,
    arrsize(bootargDebug),
    bootargBeta,
    arrsize(bootargBeta),
    KernelVersion::Tahoe,
    KernelVersion::Tahoe,
    pluginStart
};

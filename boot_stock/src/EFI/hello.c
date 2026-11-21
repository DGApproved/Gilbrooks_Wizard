/* Stock "Hello World" UEFI application for x86-64 (usbprep). Self-contained: no gnu-efi needed.
 * Installed ONLY at the removable-media fallback path \EFI\BOOT\BOOTX64.EFI on the selected USB ESP.
 * It never registers boot entries or touches NVRAM variables. Unsigned: Secure Boot must be off.
 * Behaviour: print greeting, wait for Enter, cold reset via ResetSystem. */
typedef unsigned long long UINTN;
typedef unsigned long long UINT64;
typedef unsigned int UINT32;
typedef unsigned short UINT16;
typedef unsigned short CHAR16;
typedef UINTN EFI_STATUS;
typedef void *EFI_HANDLE;
typedef void *EFI_EVENT;

typedef struct { UINT64 Signature; UINT32 Revision, HeaderSize, CRC32, Reserved; } EFI_TABLE_HEADER;
typedef struct { UINT16 ScanCode; CHAR16 UnicodeChar; } EFI_INPUT_KEY;

typedef struct SIMPLE_INPUT {
    EFI_STATUS (*Reset)(struct SIMPLE_INPUT *, unsigned char);
    EFI_STATUS (*ReadKeyStroke)(struct SIMPLE_INPUT *, EFI_INPUT_KEY *);
    EFI_EVENT WaitForKey;
} SIMPLE_INPUT;

typedef struct SIMPLE_OUTPUT {
    EFI_STATUS (*Reset)(struct SIMPLE_OUTPUT *, unsigned char);
    EFI_STATUS (*OutputString)(struct SIMPLE_OUTPUT *, CHAR16 *);
} SIMPLE_OUTPUT;

typedef struct {
    EFI_TABLE_HEADER Hdr;
    void *GetTime, *SetTime, *GetWakeupTime, *SetWakeupTime, *SetVirtualAddressMap, *ConvertPointer;
    void *GetVariable, *GetNextVariableName, *SetVariable, *GetNextHighMonotonicCount;
    void (*ResetSystem)(UINT32 type, EFI_STATUS status, UINTN size, void *data);
} RUNTIME_SERVICES;

typedef struct {
    EFI_TABLE_HEADER Hdr;
    void *RaiseTPL, *RestoreTPL, *AllocatePages, *FreePages, *GetMemoryMap, *AllocatePool, *FreePool;
    void *CreateEvent, *SetTimer;
    EFI_STATUS (*WaitForEvent)(UINTN n, EFI_EVENT *events, UINTN *index);
} BOOT_SERVICES;

typedef struct {
    EFI_TABLE_HEADER Hdr;
    CHAR16 *FirmwareVendor;
    UINT32 FirmwareRevision;
    EFI_HANDLE ConsoleInHandle;
    SIMPLE_INPUT *ConIn;
    EFI_HANDLE ConsoleOutHandle;
    SIMPLE_OUTPUT *ConOut;
    EFI_HANDLE StandardErrorHandle;
    SIMPLE_OUTPUT *StdErr;
    RUNTIME_SERVICES *RuntimeServices;
    BOOT_SERVICES *BootServices;
} SYSTEM_TABLE;

EFI_STATUS efi_main(EFI_HANDLE image, SYSTEM_TABLE *st)
{
    EFI_INPUT_KEY key;
    UINTN idx;
    (void)image;
    st->ConIn->Reset(st->ConIn, 0);
    st->ConOut->OutputString(st->ConOut, (CHAR16 *)L"Hello World\r\nPress Enter to reboot\r\n");
    for (;;) {
        st->BootServices->WaitForEvent(1, &st->ConIn->WaitForKey, &idx);
        if (st->ConIn->ReadKeyStroke(st->ConIn, &key) == 0 && key.UnicodeChar == L'\r')
            break;
    }
    st->RuntimeServices->ResetSystem(0 /* EfiResetCold */, 0, 0, 0);
    return 0;
}

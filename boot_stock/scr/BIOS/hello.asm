; Stock "Hello World" legacy-BIOS MBR boot code (usbprep). Assembles to <=440 bytes, which are
; written ONLY into bytes 0-439 of the selected USB's MBR; the disk signature, partition entries
; and the 0x55AA marker (bytes 440-511) and the GPT (LBA 1+) are left untouched.
; Behaviour: print greeting, wait for Enter, reboot (keyboard-controller reset, then BIOS warm reset).
bits 16
org 0x7C00
start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7C00
    sti
    cld
    mov ax, 0x0003          ; 80x25 text mode, clears screen
    int 0x10
    mov si, msg
.print:
    lodsb
    test al, al
    jz .wait
    mov ah, 0x0E
    mov bx, 0x0007
    int 0x10
    jmp .print
.wait:
    xor ah, ah              ; BIOS keyboard: wait for key
    int 0x16
    cmp al, 0x0D            ; Enter
    jne .wait
.reboot:
    in al, 0x64             ; wait for 8042 input buffer empty
    test al, 2
    jnz .reboot
    mov al, 0xFE            ; pulse CPU reset line
    out 0x64, al
    jmp 0xFFFF:0x0000       ; fallback: BIOS warm reset vector
msg: db "Hello World", 13, 10, "Press Enter to reboot", 13, 10, 0

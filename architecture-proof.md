# Virtio PCI mapping attributes

Baseline: `b7a1d910fcb876500e584761ed8bb1c25c8cd5e7`.

## Driver and API boundary

`K_MEM_PERM_RW = BIT(3)`, `K_MEM_CACHE_WB = 0`,
`K_MEM_CACHE_NONE = 2`, and `K_MEM_CACHE_MASK = 7`.
The original flags `0x8` select write-back. The fixed flags `0xa` select
non-cacheable memory with read/write access.

All four register capabilities in `virtio_pci_init_common()` share
`virtio_pci_map_cap()`:

| Capability | Baseline call line |
| --- | --- |
| Common configuration | 436 |
| ISR status | 449 |
| Queue notification | 462 |
| Device-specific configuration | 485 |

The driver maps `mbar.phys_addr + cap->offset` for `cap->length` bytes.
These expressions are identical in the fix. The non-MMU path is also identical.
The kernel page-aligns the range and forwards the flags to `arch_mem_map()`.

Source references:
- [Cache and permission flags](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/include/zephyr/kernel/mm.h#L36)
- [Driver helper and call sites](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/drivers/virtio/virtio_pci.c#L323)
- [Kernel mapping](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/kernel/mmu.c#L1115)

## Architecture conversion

| Architecture | Original flags 0x8 | Fixed flags 0xa |
| --- | --- | --- |
| ARM64 | MT_NORMAL, AttrIndx 4, MAIR byte 0xff | MT_DEVICE_nGnRnE, AttrIndx 0, MAIR byte 0x00 |
| AArch32 | MT_NORMAL, inner/outer write-back with write allocate | Shareable MT_DEVICE |
| x86 | PCD = 0, PWT = 0 | PCD = 1, PWT = 0 |

Each path preserves read/write permission. In ARM64,
`K_MEM_CACHE_NONE` and `K_MEM_ARM_DEVICE_nGnRnE` have the same value.
The x86 converter sets PCD for `K_MEM_CACHE_NONE` without changing PAT.
MTRRs and platform configuration also determine the effective x86 memory type.
A functional x86 pass can therefore coexist with the original incorrect cache
request. A page-table measurement proves the PCD bit selected by the mapping
code, rather than the complete effective hardware memory type.

Source references:
- [ARM64 flags conversion](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/arch/arm64/core/mmu.c#L1331)
- [ARM64 MAIR encodings](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/include/zephyr/arch/arm64/arm_mmu.h#L22)
- [AArch32 flags conversion](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/arch/arm/core/mmu/arm_mmu.c#L996)
- [x86 flags conversion](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/arch/x86/core/x86_mmu.c#L1204)
- [x86 PCD encoding](https://github.com/zephyrproject-rtos/zephyr/blob/b7a1d910fcb876500e584761ed8bb1c25c8cd5e7/arch/x86/include/x86_mmu.h#L37)

This document records source inspection. The executable harness and original
serial logs provide the separate runtime evidence.


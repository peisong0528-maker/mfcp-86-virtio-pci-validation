#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate a test-only patch around the actual virtio PCI mapping helper."""

import argparse
import difflib
from pathlib import Path
import subprocess

DRIVER = "drivers/virtio/virtio_pci.c"
CMAKE = "drivers/virtio/CMakeLists.txt"
SCENARIOS = "tests/drivers/disk/disk_access/tests.yaml"
HELPER = "static bool virtio_pci_map_cap("
NEXT_FUNCTION = "static uint32_t virtio_pci_read_device_feature_word("

WRAPPER_START = r"""
#if defined(CONFIG_MMU)
static void mfcp86_map_cap(pcie_bdf_t bdf, uint8_t cfg_type, uint8_t **virt_ptr,
                           uintptr_t phys, size_t length, uint32_t flags)
{
"""
CAPTURE = r"""	k_mem_map_phys_bare(virt_ptr, phys, length, flags);
	printk("MFCP86 MAP bdf=0x%x cap=%u phys=0x%llx len=%zu flags=0x%x\n",
	       (unsigned int)bdf, (unsigned int)cfg_type, (unsigned long long)phys,
	       length, (unsigned int)flags);
"""
PTE_CAPTURE = r"""	int level;
	pentry_t entry;

	k_mem_map_phys_bare(virt_ptr, phys, length, flags);
	z_x86_pentry_get(&level, &entry, z_x86_kernel_ptables, *virt_ptr);
	printk("MFCP86 MAP bdf=0x%x cap=%u phys=0x%llx len=%zu flags=0x%x "
	       "p=%u rw=%u pcd=%u pwt=%u\n",
	       (unsigned int)bdf, (unsigned int)cfg_type, (unsigned long long)phys,
	       length, (unsigned int)flags,
	       (unsigned int)!!(entry & MMU_P), (unsigned int)!!(entry & MMU_RW),
	       (unsigned int)!!(entry & MMU_PCD), (unsigned int)!!(entry & MMU_PWT));
"""
WRAPPER_END = r"""}

#define k_mem_map_phys_bare(out, phys, length, flags) \
	mfcp86_map_cap(bdf, cap->cfg_type, out, phys, length, flags)
#endif

"""
EVENT_HELPER = r"""
static void mfcp86_event(const struct device *dev, const char *kind)
{
	const struct virtio_pci_config *conf = dev->config;

	printk("MFCP86 EVENT bdf=0x%x kind=%s\n", (unsigned int)conf->pcie->bdf, kind);
}

"""


def replace_once(content, old, new, description):
    if content.count(old) != 1:
        raise ValueError(f"Expected exactly one {description}")
    return content.replace(old, new, 1)


def instrument_driver(content, x86_pte):
    if "MFCP86 MAP " in content:
        raise ValueError("Driver is already instrumented")
    if content.count(HELPER) != 1 or content.count(NEXT_FUNCTION) != 1:
        raise ValueError("Cannot identify the mapping helper")
    start = content.index(HELPER)
    stop = content.index(NEXT_FUNCTION, start)
    if content[start:stop].count("k_mem_map_phys_bare(") != 1:
        raise ValueError("Expected one mapper call in the actual helper")

    include_anchor = '#include "assert.h"\n'
    includes = "#include <zephyr/sys/printk.h>\n"
    if x86_pte:
        includes += "#include <x86_mmu.h>\n"
    content = replace_once(content, include_anchor, include_anchor + includes, "include anchor")
    wrapper = WRAPPER_START + (PTE_CAPTURE if x86_pte else CAPTURE) + WRAPPER_END
    content = replace_once(content, HELPER, wrapper + HELPER, "mapping helper")
    content = replace_once(
        content, NEXT_FUNCTION,
        "#if defined(CONFIG_MMU)\n#undef k_mem_map_phys_bare\n#endif\n\n" + NEXT_FUNCTION,
        "function after the mapping helper",
    )

    isr = "void virtio_pci_isr(const struct device *dev)\n"
    content = replace_once(content, isr, EVENT_HELPER + isr, "ISR definition")
    original_isr = (
        "\tvirtio_isr(dev, *data->isr_status, data->virtqueue_count);\n\n"
        "\tk_spin_unlock(&data->isr_lock, key);\n"
    )
    observed_isr = (
        "\tuint8_t status = *data->isr_status;\n\n"
        "\tvirtio_isr(dev, status, data->virtqueue_count);\n"
        "\tk_spin_unlock(&data->isr_lock, key);\n"
        "\tif ((status & BIT(0)) != 0U) {\n"
        '\t\tmfcp86_event(dev, "irq");\n'
        "\t}\n"
    )
    content = replace_once(content, original_isr, observed_isr, "single ISR status read")
    notify_unlock = "\tk_spin_unlock(&data->notify_lock, key);\n"
    content = replace_once(
        content, notify_unlock, notify_unlock + '\tmfcp86_event(dev, "notify");\n',
        "notification unlock",
    )
    driver_ok = "\tvirtio_pci_write_status_bit(dev, DEVICE_STATUS_DRIVER_OK);\n"
    return replace_once(
        content, driver_ok, driver_ok + '\tmfcp86_event(dev, "init");\n', "DRIVER_OK"
    )


def make_diff(path, old, new):
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            f"a/{path}", f"b/{path}",
        )
    )


def git(worktree, *args):
    return subprocess.check_output(
        ["git", "-C", str(worktree), *args], text=True, encoding="utf-8"
    ).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", type=Path)
    parser.add_argument("patch", type=Path)
    parser.add_argument("--x86-pte", action="store_true")
    args = parser.parse_args()
    worktree, patch = args.worktree.resolve(), args.patch.resolve()
    try:
        if Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
            raise ValueError("Pass the Zephyr worktree root")
        changed = set(git(worktree, "diff", "--name-only", "HEAD").splitlines())
        if changed - {DRIVER}:
            raise ValueError(f"Unexpected tracked changes: {sorted(changed - {DRIVER})}")
        if patch.is_relative_to(worktree) or patch.exists():
            raise ValueError("Use a new patch path outside the worktree")
        driver = (worktree / DRIVER).read_text(encoding="utf-8")
        changes = [make_diff(DRIVER, driver, instrument_driver(driver, args.x86_pte))]
        if args.x86_pte:
            cmake = (worktree / CMAKE).read_text(encoding="utf-8")
            cmake_with_probe = replace_once(
                cmake, "zephyr_library()\n",
                "zephyr_library()\n"
                "if(CONFIG_X86_64 AND CONFIG_EXCEPTION_DEBUG)\n"
                "  zephyr_library_include_directories(${ZEPHYR_BASE}/arch/x86/include)\n"
                "endif()\n",
                "virtio CMake target",
            )
            changes.append(make_diff(CMAKE, cmake, cmake_with_probe))
            scenarios = (worktree / SCENARIOS).read_text(encoding="utf-8")
            scenario_anchor = (
                "  drivers.disk.virtio_blk:\n"
                "    extra_args: FILE_SUFFIX=virtio_blk\n"
                "    extra_configs:\n"
            )
            scenarios_with_probe = replace_once(
                scenarios, scenario_anchor,
                scenario_anchor + "      - CONFIG_EXCEPTION_DEBUG=y\n",
                "virtio-blk scenario",
            )
            changes.append(make_diff(SCENARIOS, scenarios, scenarios_with_probe))
        patch.write_text("".join(changes), encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(worktree), "apply", "--check", str(patch)], check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Validated test patch at {git(worktree, 'rev-parse', 'HEAD')}")


if __name__ == "__main__":
    main()


# MFCP #86 validation

Reproducible evidence for the Zephyr virtio PCI register mapping fix.
The upstream change is the small [fix.patch](fix.patch). The capture tools and
recorded results support [MFCP task #86](https://github.com/missionhub-oss/mfcp/issues/86)
and [Zephyr issue #123755](https://github.com/zephyrproject-rtos/zephyr/issues/123755).

The baseline is `b7a1d910fcb876500e584761ed8bb1c25c8cd5e7`.
ACPICA is pinned by that baseline to `8d24867bc9c9d81c81eeac59391cda59333affd4`.

## Recorded results

The [recorded run](evidence/validation-02) completed on Linux x86_64 with
Zephyr SDK 1.0.1, GCC 14.3.0, QEMU 10.0.2, Python 3.12.3, west 1.5.0,
CMake 3.31.10 and Ninja 1.11.1.

| Check | Original driver | Fixed driver |
| --- | --- | --- |
| Virtio build-all | Passed | Passed |
| Virtio-blk functional scenario | 2 passed, 1 erase case skipped | 2 passed, 1 erase case skipped |
| Instrumented virtio-blk scenario | 2 passed, 1 erase case skipped | 2 passed, 1 erase case skipped |
| Four mapping API requests | flags 0x8 | flags 0xa |
| Corresponding x86 page-table entries | PCD 0 | PCD 1 |
| Initialization, notification and interrupt events | Observed | Observed |

For each capability, the physical address and length match across both runs.
The present and read/write bits remain set, and PWT remains clear.
The saved configurations match pairwise between the original and fixed runs.
Published logs use a generic workspace prefix as documented in
[REDACTIONS.md](evidence/validation-02/REDACTIONS.md).
The negative control feeds unfixed data to the fixed expectations and is rejected.

Read [comparison.txt](evidence/validation-02/comparison.txt),
[the negative control](evidence/validation-02/negative-control.log),
and the per-run JSON reports, configurations and serial logs.

## Reproduce

Use a Linux x86_64 development host with Zephyr SDK 1.0.1 and the GNU
`x86_64-zephyr-elf` toolchain. Install the Python versions in
[evidence/validation-02/python-packages.txt](evidence/validation-02/python-packages.txt)
in a virtual environment, with Ninja and gperf available.
Set `ZEPHYR_SDK_INSTALL_DIR` to the SDK directory.

Check out the manifest repository at the baseline commit before running
`west init -l zephyr` and `west update acpica`. This ensures that the module
revision comes from the tested manifest. Use the baseline as the source tree.
Then run one command from this repository:

```sh
bash run.sh /absolute/path/to/workspace/zephyr /absolute/path/to/new-results
```

The output directory must be new. The script creates two disposable Git
worktrees inside the west workspace, applies the fix only to the after tree,
and retains all logs and working copies. Build parallelism defaults to four
jobs and can be set with `MFCP86_JOBS`.

Each tree first runs a clean build and functional test. The same instrumenter
then generates a capture patch, and the functional test runs again:

```sh
west build -b qemu_x86_64 tests/drivers/build_all/virtio
west twister -p qemu_x86_64 -T tests/drivers/disk/disk_access -s drivers.disk.virtio_blk
```

The script specifies separate output directories and checks that the actual
source tree and required `CONFIG_MMU=y` and `CONFIG_VIRTIO_PCI=y` settings
appear in the generated build metadata. It also requires one explicitly
passed scenario in each Twister JSON report.

At this baseline, Twister defaults to short scenario IDs. The original
full-path `-s` invocation selected zero suites. Its
[attempt log](evidence/validation-02/setup-notes/unselected-full-scenario.log)
is retained. The explicit `-T` directory and short `-s` ID above select
the intended scenario.

## Capture boundary

[instrument.py](instrument.py) generates a patch for a disposable worktree.
A preprocessor wrapper surrounds only the actual `virtio_pci_map_cap()`
definition and receives the evaluated arguments of its mapper call.
The wrapper records the capability type, physical address, length and flags,
and forwards those arguments to the real `k_mem_map_phys_bare()`.

With `--x86-pte`, it enables the existing `CONFIG_EXCEPTION_DEBUG` option
for the test and calls the existing `z_x86_pentry_get()` accessor after
mapping. The comparator checks P, RW, PCD and PWT.

QEMU provides the PCI Virtio device. The driver, mapping implementation and
page tables execute in Zephyr. Functional markers record DRIVER_OK, queue
notification, and queue interrupts. The ISR uses its original single
read-to-clear status access and reuses that value for the marker.

[compare.py](compare.py) requires all four capabilities and the three
functional event types from the same PCI device. It rejects missing,
malformed or duplicate mapping records and mismatched before/after addresses,
lengths or flags.

## Architecture evidence

[architecture-proof.md](architecture-proof.md) traces the original and fixed
flags through ARM64, AArch32 and x86 implementations at the fixed baseline.
ARM64 and AArch32 conclusions are source inspection. Execution evidence is
from qemu_x86_64. The x86 PCD observation verifies the page-table bit, while
the effective memory type also depends on MTRR and PAT configuration.

Only the register flags fix is proposed upstream. The capture patches,
debug output and reports are maintained in this task repository.

## License

The validation scripts use Apache-2.0. Generated patches retain the license
and attribution of their upstream source files. See [LICENSE](LICENSE).


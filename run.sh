#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 /path/to/west-workspace/zephyr /new/evidence/directory" >&2
    exit 2
fi

source_tree=$(realpath "$1")
output=$(realpath -m "$2")
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
base=b7a1d910fcb876500e584761ed8bb1c25c8cd5e7
scenario=drivers.disk.virtio_blk
test_dir=tests/drivers/disk/disk_access
jobs=${MFCP86_JOBS:-4}
sdk=${ZEPHYR_SDK_INSTALL_DIR:?Set ZEPHYR_SDK_INSTALL_DIR to Zephyr SDK 1.0.1}

if [[ -e "$output" ]]; then
    echo "Use a new evidence directory: $output" >&2
    exit 2
fi
if [[ "$(git -C "$source_tree" rev-parse --show-toplevel)" != "$source_tree" ]]; then
    echo "The source argument must be the repository root." >&2
    exit 2
fi
git -C "$source_tree" cat-file -e "$base^{commit}"
workspace=$(cd "$source_tree" && west topdir)
case "$source_tree/" in
    "$workspace/"*) ;;
    *) echo "Source is outside its west workspace." >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$output")"
mkdir "$output"
worktrees=$(mktemp -d "$workspace/.mfcp86-worktrees.XXXXXX")
printf '%s\n' "$worktrees" > "$output/worktrees.txt"
trap 'echo "Validation stopped at line $LINENO. Logs are in $output." >&2' ERR
export QEMU_BIN_PATH="$sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin"
export PATH="$QEMU_BIN_PATH:$PATH"
export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"

{
    printf 'Zephyr baseline %s\n' "$base"
    uname -srm
    python3 --version
    west --version
    cmake --version
    ninja --version
    "$sdk/gnu/x86_64-zephyr-elf/bin/x86_64-zephyr-elf-gcc" --version
    "$QEMU_BIN_PATH/qemu-system-x86_64" --version
    printf 'SDK version '
    cat "$sdk/sdk_version"
} > "$output/environment.txt" 2>&1
python3 -m pip freeze > "$output/python-packages.txt"
cp "$script_dir/fix.patch" "$output/fix.patch"
sha256sum "$script_dir/instrument.py" "$script_dir/compare.py" "$script_dir/run.sh" \
    "$script_dir/fix.patch" > "$output/validation-tools.sha256"

git -C "$source_tree" worktree add --detach "$worktrees/before" "$base" > "$output/worktree-before.log" 2>&1
git -C "$source_tree" worktree add --detach "$worktrees/after" "$base" > "$output/worktree-after.log" 2>&1
git -C "$worktrees/after" apply --check "$script_dir/fix.patch"
git -C "$worktrees/after" apply "$script_dir/fix.patch"

record_config()
{
    python3 - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import shutil
import sys

configs = list(Path(sys.argv[1]).rglob("zephyr/.config"))
if len(configs) != 1:
    raise SystemExit(f"Expected one build configuration, found {len(configs)}")
config = configs[0]
lines = config.read_text().splitlines()
for required in ("CONFIG_MMU=y", "CONFIG_VIRTIO_PCI=y"):
    if required not in lines:
        raise SystemExit(f"{config}: missing {required}")
cache = (config.parent.parent / "CMakeCache.txt").read_text().splitlines()
expected_base = f"ZEPHYR_BASE:PATH={sys.argv[3]}"
if expected_base not in cache:
    raise SystemExit(f"Build used a different source tree: {config}")
shutil.copy2(config, sys.argv[2])
PY
}

verify_twister()
{
    python3 - "$1/twister.json" <<'PY'
import json
from pathlib import Path
import sys

report = json.loads(Path(sys.argv[1]).read_text())
suites = report.get("testsuites", [])
if len(suites) != 1:
    raise SystemExit(f"Expected one selected suite, found {len(suites)}")
suite = suites[0]
if suite.get("name") != "drivers.disk.virtio_blk" or suite.get("status") != "passed":
    raise SystemExit(f"Virtio-blk did not run successfully: {suite.get('status')}")
print(json.dumps({
    "name": suite["name"],
    "status": suite["status"],
    "platform": suite.get("platform"),
    "testcases": [{key: case.get(key) for key in ("identifier", "status")}
                  for case in suite.get("testcases", [])],
}, indent=2))
PY
}

for variant in before after; do
    tree="$worktrees/$variant"
    export ZEPHYR_BASE="$tree"
    variant_out="$output/$variant"
    mkdir "$variant_out"
    git -C "$tree" diff --no-ext-diff > "$variant_out/source.patch"

    echo "[$variant] clean Virtio build"
    (
        cd "$tree"
        west build -b qemu_x86_64 tests/drivers/build_all/virtio \
            -d "$variant_out/build-all" --build-opt="-j$jobs"
    ) > "$variant_out/build-all.log" 2>&1
    record_config "$variant_out/build-all" "$variant_out/build-all.config" "$tree"

    echo "[$variant] clean virtio-blk functional regression"
    (
        cd "$tree"
        west twister -p qemu_x86_64 -T "$test_dir" -s "$scenario" -j "$jobs" -i \
            -O "$variant_out/functional"
    ) > "$variant_out/functional.log" 2>&1
    record_config "$variant_out/functional" "$variant_out/functional.config" "$tree"
    verify_twister "$variant_out/functional" > "$variant_out/functional-summary.json"

    echo "[$variant] real mapping API and x86 PTE capture"
    python3 "$script_dir/instrument.py" "$tree" \
        "$variant_out/capture.patch" --x86-pte > "$variant_out/instrument.log" 2>&1
    git -C "$tree" apply "$variant_out/capture.patch"
    (
        cd "$tree"
        west twister -p qemu_x86_64 -T "$test_dir" -s "$scenario" -j "$jobs" -i \
            -O "$variant_out/capture"
    ) > "$variant_out/capture.log" 2>&1
    record_config "$variant_out/capture" "$variant_out/capture.config" "$tree"
    verify_twister "$variant_out/capture" > "$variant_out/capture-summary.json"
    mapfile -t serial_logs < <(find "$variant_out/capture" -type f -name handler.log)
    if [[ ${#serial_logs[@]} -ne 1 ]]; then
        echo "Expected one capture serial log for $variant." >&2
        exit 1
    fi
    cp "${serial_logs[0]}" "$variant_out/serial.log"
done

for config in build-all functional capture; do
    cmp "$output/before/$config.config" "$output/after/$config.config"
done
printf 'PASS: matching before/after configurations\\n' > "$output/configuration-comparison.txt"

python3 "$script_dir/compare.py" "$output/before/serial.log" \
    "$output/after/serial.log" --require-pte | tee "$output/comparison.txt"

if python3 "$script_dir/compare.py" "$output/before/serial.log" \
    "$output/before/serial.log" --require-pte > "$output/negative-control.log" 2>&1; then
    echo "Unfixed data unexpectedly passed the fixed expectations." >&2
    exit 1
fi
printf 'PASS: unfixed data rejected by fixed expectations\n' >> "$output/negative-control.log"
printf 'MFCP86_VALIDATION_PASS\n' | tee "$output/result.txt"


#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ksound_hub.module_dev import (  # noqa: E402
    ModuleWorkContract,
    build_work_contract,
    classify_touched_paths,
    dump_contract,
    verify_work_contract,
)
from ksound_hub.module_registry import DEFAULT_MODULE_IDS, ModuleRegistry  # noqa: E402


def registry_for(repo: Path) -> ModuleRegistry:
    settings = SimpleNamespace(
        installed_modules=list(DEFAULT_MODULE_IDS),
        enabled_modules=list(DEFAULT_MODULE_IDS),
    )
    return ModuleRegistry(settings, external_root=repo / ".ksounds-dev-empty-modules")


def manifest_or_die(registry: ModuleRegistry, module_id: str):
    manifest = registry.manifest(module_id)
    if manifest is None:
        raise SystemExit(f"unknown bundled module: {module_id}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="K-Sounds module development lane guard")
    parser.add_argument("--repo", default=str(ROOT), help="K-Sounds repository root")
    sub = parser.add_subparsers(dest="command", required=True)

    desc = sub.add_parser("describe", help="show one module's development boundary")
    desc.add_argument("module")
    desc.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="classify touched files for one module lane")
    check.add_argument("module")
    check.add_argument("paths", nargs="+")
    check.add_argument("--allow-shared", action="store_true")

    snap = sub.add_parser("snapshot", help="emit a module-scoped base contract")
    snap.add_argument("module")
    snap.add_argument("--output")

    verify = sub.add_parser("verify", help="verify a module-scoped base contract")
    verify.add_argument("module")
    verify.add_argument("contract")
    verify.add_argument("--check-shared", action="store_true")

    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    registry = registry_for(repo)
    manifest = manifest_or_die(registry, args.module)

    if args.command == "describe":
        payload = {
            "module": manifest.module_id,
            "version": manifest.version,
            "ksounds_api": manifest.ksounds_api,
            "owned_paths": list(manifest.owned_paths),
            "shared_paths": list(manifest.shared_paths),
            "parallel_rule": "owned-only patches are independent; shared/foreign paths require the Core lane",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"{manifest.module_id} v{manifest.version} · ksounds_api={manifest.ksounds_api}")
            print("owned:")
            for path in manifest.owned_paths:
                print(f"  + {path}")
            print("shared/core:")
            for path in manifest.shared_paths:
                print(f"  ! {path}")
        return 0

    if args.command == "check":
        result = classify_touched_paths(manifest, args.paths)
        for path in result.owned:
            print(f"OWNED   {path}")
        for path in result.shared:
            print(f"SHARED  {path}")
        for path in result.foreign:
            print(f"FOREIGN {path}")
        if result.foreign:
            print("module_lane=blocked reason=foreign_path", file=sys.stderr)
            return 3
        if result.shared and not args.allow_shared:
            print("module_lane=core_required reason=shared_path", file=sys.stderr)
            return 2
        print("module_lane=ok")
        return 0

    if args.command == "snapshot":
        contract = build_work_contract(repo, manifest)
        rendered = dump_contract(contract)
        if args.output:
            path = Path(args.output).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print(path)
        else:
            print(rendered, end="")
        return 0

    raw = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    contract = ModuleWorkContract.from_dict(raw)
    errors = verify_work_contract(repo, manifest, contract, check_shared=args.check_shared)
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 4
    print(
        f"module_contract=ok module={manifest.module_id} version={manifest.version} "
        f"ksounds_api={manifest.ksounds_api} shared_checked={int(args.check_shared)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

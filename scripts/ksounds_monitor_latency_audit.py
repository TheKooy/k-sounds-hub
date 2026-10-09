#!/usr/bin/env python3
"""Read-only K-Sounds/PipeWire latency budget snapshot (IP14n10b).

No devices are opened; does not change streams, buffers, service state or volume.
The Pulse compatibility fields are estimates, NOT mouth-to-headset measurements.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def pactl_json(kind: str):
    try:
        result = subprocess.run(
            ["pactl", "--format=json", "list", kind],
            check=True, capture_output=True, text=True, timeout=5,
        )
        value = json.loads(result.stdout)
        return value if isinstance(value, list) else []
    except (FileNotFoundError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"  {kind}: unavailable ({type(exc).__name__})")
        return []


def native_processes() -> list[tuple[int, list[str]]]:
    out: list[tuple[int, list[str]]] = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\x00')
        except OSError:
            continue
        if any('ksound_native_micro_engine' in s for s in command[:1]) and '--state' in command:
            out.append((int(entry.name), command))
    return out


def native_children(pids: list[int]) -> set[str]:
    children: set[str] = set()
    for pid in pids:
        try:
            content = Path(f'/proc/{pid}/task/{pid}/children').read_text()
            children.update(content.split())
        except OSError:
            continue
    return children


def stream_name(item: dict) -> str:
    props = item.get('properties') or {}
    return str(props.get('application.name') or props.get('media.name') or props.get('node.name') or 'unknown')


def print_latency(item: dict):
    for key in ('buffer_usec', 'sink_usec', 'source_usec', 'latency', 'requested_latency', 'configured_latency'):
        if key in item:
            print(f'    {key}: {item[key]}')
    props = item.get('properties') or {}
    for key in ('node.latency', 'node.rate', 'pulse.min.req', 'pulse.min.frag', 'pulse.min.quantum'):
        if key in props:
            print(f'    {key}: {props[key]}')
    if 'corked' in item:
        print(f"    corked: {item['corked']}")


def show_streams(kind: str, streams: list[dict], children: set[str], sinks: dict, sources: dict):
    print(f'\n=== {kind.upper()} (K-Sounds-relevant streams) ===')
    selected = 0
    for item in streams:
        if not isinstance(item, dict):
            continue
        props = item.get('properties') or {}
        pid = str(props.get('application.process.id') or '')
        name = stream_name(item)
        sink = sinks.get(item.get('sink'), '')
        source = sources.get(item.get('source'), '')
        text = f'{name} {sink} {source} {props.get("media.name", "")}'.lower()
        if not (pid in children or any(s in text for s in ('ksound', 'k-sound', 'return-mic', 'micro_bus', 'retour'))):
            continue
        selected += 1
        print(f"  #{item.get('index', '?')}: {name} (process {pid or '?'})")
        if sink:
            print(f'    destination sink: {sink}')
        if source:
            print(f'    capture source: {source}')
        print_latency(item)
    if not selected:
        print('  No matching stream found (stream names may vary by Pulse/PipeWire version).')


def print_logs(native: list[tuple[int, list[str]]]):
    print('\n=== NATIVE ENGINE TIMING: LAST 3 INTERVALS ===')
    path = None
    for _, args in native:
        if '--log' in args:
            i = args.index('--log')
            if i + 1 < len(args):
                path = Path(args[i + 1])
                break
    if path is None:
        path = Path.home() / '.config/k-sounds-hub/runtime/native-engine/native-micro-engine.log'
    try:
        # Bounded tail avoids reading a potentially huge log file into memory.
        with path.open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 100_000))
            text = f.read().decode(errors='replace')
        lines = [line for line in text.splitlines() if 'stable-monitor 30s' in line]
        if lines:
            for line in lines[-3:]:
                print('  ' + line)
        else:
            print('  Wait ~30 seconds with the native engine running for its first interval.')
    except OSError:
        print('  Native log not accessible.')



def return_mic_eq_requests(proc_root: Path = Path('/proc')) -> list[tuple[int, str]]:
    """Read only EQ-owned process metadata, without exposing unrelated env vars."""
    found: list[tuple[int, str]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes().split(b'\0')
            if not cmd or not cmd[0].endswith(b'pipewire') or b'filter-chain.conf' not in cmd:
                continue
            raw_env = (entry / 'environ').read_bytes().split(b'\0')
            config_homes = [v.partition(b'=')[2] for v in raw_env if v.startswith(b'XDG_CONFIG_HOME=')]
            if not config_homes or not config_homes[-1].endswith(b'/return-mic-eq-xdg'):
                continue
            latency = next((v.partition(b'=')[2].decode(errors='replace')
                            for v in raw_env if v.startswith(b'PIPEWIRE_LATENCY=')), '(not specified)')
            found.append((int(entry.name), latency))
        except (OSError, ValueError):
            continue
    return sorted(found)


def print_return_mic_eq_profile() -> None:
    print('\n=== RETURN-MIC EQ FILTER LATENCY REQUEST ===')
    eqs = return_mic_eq_requests()
    if not eqs:
        print('  No independently identifiable RETURN-MIC EQ process found.')
    else:
        for pid, requested in eqs:
            print(f'  PID {pid}: PIPEWIRE_LATENCY={requested} (request, NOT measured acoustic delay)')
        if len(eqs) > 1:
            print('  WARNING: multiple RETURN-MIC EQ processes: verify there is no duplicate route.')

def main():
    print('K-Sounds IP14n10c — read-only transport and latency budget snapshot')
    print('Requested/estimated latencies are NOT acoustic or end-to-end measurements.')
    native = native_processes()
    if native:
        print('Native engine PIDs:', ', '.join(str(pid) for pid, _ in native))
    else:
        print('Native engine not found; stream audit may be incomplete.')
    children = native_children([pid for pid, _ in native])
    print('Native audio child PIDs:', ', '.join(sorted(children)) or '(none detected)')
    sinks_list = pactl_json('sinks')
    sources_list = pactl_json('sources')
    sinks = {x.get('index'): x.get('name', '') for x in sinks_list if isinstance(x, dict)}
    sources = {x.get('index'): x.get('name', '') for x in sources_list if isinstance(x, dict)}
    show_streams('sink-inputs', pactl_json('sink-inputs'), children, sinks, sources)
    show_streams('source-outputs', pactl_json('source-outputs'), children, sinks, sources)
    print_return_mic_eq_profile()
    print('\n=== RELEVANT SINKS (REPORTED LATENCY) ===')
    for item in sinks_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name', ''))
        if not any(s in name.lower() for s in ('micro', 'retour', 'return', 'arctis', 'steelseries')):
            continue
        print(f"  {name}")
        print_latency(item)
    print_logs(native)
    print('\nNo audio changes performed. Figures do not include USB microphone ADC or wireless headset delay.')


if __name__ == '__main__':
    main()

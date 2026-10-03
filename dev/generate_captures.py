#!/usr/bin/env python3
"""Generate bounded, deterministic SSH transcript fixtures using only the stdlib."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

MIB = 1024 ** 2
GIB = 1024 ** 3
PROFILES = {
    'smoke': [64 * 1024] * 12,
    'fleet': [4 * MIB] * 500,
    'large': [512 * MIB] * 8,
    'mixed': [size * MIB for size in (16, 64, 128, 256)] * 8,
}
VENDORS = {
    'cisco': ('show version', 'show running-config', 'show interfaces',
              'show ip route', 'show logging', 'dir flash'),
    'junos': ('show version', 'show configuration', 'show interfaces extensive',
              'show route', 'show log messages', 'request support information'),
    'panos': ('show system info', 'show config running', 'show interface all',
              'show routing route', 'show log system', 'debug dataplane internal statistics'),
}


def sections(device, vendor):
    """Return command/body/record-kind triples; markers are added independently."""
    info, config, interfaces, routes, logs, diagnostic = VENDORS[vendor]
    inventory = (f'Hostname: {device}\nImage family: synthetic-{vendor}\n'
                 'Software build: fixture-1\nSerial: SYNTHETIC-0000\n')
    configurations = {
        'cisco': (f'hostname {device}\n!\ninterface GigabitEthernet0/1\n'
                  ' description WAN\n ip address 192.0.2.10 255.255.255.0\n'
                  ' no shutdown\n!\ninterface GigabitEthernet0/2\n'
                  ' description SPARE\n shutdown\n!\n'),
        'junos': (f'system {{\n    host-name {device};\n}}\ninterfaces {{\n'
                  '    ge-0/0/0 {\n        description WAN;\n'
                  '        unit 0 {\n            family inet {\n'
                  '                address 192.0.2.10/24;\n            }\n'
                  '        }\n    }\n}\n'),
        'panos': (f'<config>\n  <deviceconfig>\n    <hostname>{device}</hostname>\n'
                  '  </deviceconfig>\n  <network>\n    <interface>\n'
                  '      <entry name="ethernet1/1">\n        <comment>WAN</comment>\n'
                  '      </entry>\n    </interface>\n  </network>\n</config>\n'),
    }
    result = [(info, inventory, None), (config, configurations[vendor], None)]
    for cycle in range(12):
        for command, kind in ((interfaces, 'interfaces'), (routes, 'routes'),
                              (logs, 'logs'), (diagnostic, 'diagnostic')):
            result.append((command, f'Synthetic snapshot {cycle + 1:02d}: {kind}\n', kind))
        if cycle in (5, 11):
            result.append((info, inventory, None))
    result.append(('exit', 'Connection closed by remote host.\n', None))
    return result


def record_block(kind, vendor, number, newline):
    """Use bounded record windows, varying counters and addresses by device."""
    lines = []
    port = {'cisco': 'GigabitEthernet0/', 'junos': 'ge-0/0/', 'panos': 'ethernet1/'}[vendor]
    for index in range(512):
        subnet = (number + index) % 256
        counter = number * 100003 + index * 997
        if kind == 'interfaces':
            lines.append(f'{port}{index % 48 + 1} is up, line protocol is up\n'
                         f'  MTU 1500, input packets {counter}, output packets {counter + 321}\n'
                         f'  input errors {index % 7}, drops {index % 3}, description synthetic-link-{subnet:03d}\n')
        elif kind == 'routes':
            lines.append(f'O 10.{number % 256}.{subnet}.0/24 [110/{index % 50 + 1}] '
                         f'via 192.0.2.{index % 200 + 1}, 00:{index % 60:02d}:12, {port}{index % 48 + 1}\n')
        elif kind == 'logs':
            lines.append(f'2026-01-01T12:{index % 60:02d}:{index % 60:02d}Z '
                         f'device-{number:04d} INFO interface={port}{index % 48 + 1} '
                         f'event=link-state state=up sequence={counter}\n')
        else:
            lines.append(f'counter_{index:04d} packets={counter} bytes={counter * 64} '
                         f'errors={index % 5} queue={index % 8} status=normal\n')
    return ''.join(lines).replace('\n', newline).encode('ascii')


def write_capture(path, size, number, vendor):
    device = path.stem
    newline = '\r\n' if number % 2 else '\n'
    prompt = f'{device}#' if vendor == 'cisco' else f'admin@{device}>'
    separator = '' if vendor == 'cisco' else ' '
    encode = lambda value: value.replace('\n', newline).encode('ascii')
    banner = encode('Synthetic SSH session; no real device data.\nAuthentication successful.\n')
    entries = []
    for index, (command, body, kind) in enumerate(sections(device, vendor)):
        marker = f'SPCHECK device={device} section={index:02d} command={command}'
        prefix = encode(f'{prompt}{separator}{command}\n{body}')
        suffix = encode(f'\n{marker}\n')
        entries.append((command, prefix, suffix, kind, marker))
    fixed = len(banner) + sum(len(prefix) + len(suffix) for _, prefix, suffix, _, _ in entries)
    if size < fixed:
        raise ValueError(f'Capture size must be at least {fixed} bytes')
    bulk_count = sum(kind is not None for _, _, _, kind, _ in entries)
    per_bulk, remainder = divmod(size - fixed, bulk_count)
    blocks = {kind: record_block(kind, vendor, number, newline)
              for kind in ('interfaces', 'routes', 'logs', 'diagnostic')}
    digest = hashlib.sha256()
    written = 0
    with path.open('xb') as stream:
        def emit(data):
            nonlocal written
            stream.write(data)
            digest.update(data)
            written += len(data)
        emit(banner)
        for _, prefix, suffix, kind, _ in entries:
            emit(prefix)
            if kind:
                budget = per_bulk + bool(remainder)
                remainder = max(0, remainder - 1)
                block = blocks[kind]
                while budget >= len(block):
                    emit(block)
                    budget -= len(block)
                # Retain complete records, then pad less than one record's length.
                tail = block[:budget]
                boundary = tail.rfind(b'\n') + 1
                emit(tail[:boundary])
                emit(b' ' * (budget - boundary))
            emit(suffix)
    assert written == size
    latest = path.with_suffix('.show')
    latest.symlink_to(path.name)
    return {
        'path': path.name, 'latest': latest.name, 'bytes': size,
        'sha256': digest.hexdigest(), 'vendor': vendor,
        'first_marker': entries[0][4], 'exit_marker': entries[-1][4],
        'show_markers': [entry[4] for entry in entries if entry[0].startswith('show')],
        'diagnostic_command': VENDORS[vendor][-1],
        'diagnostic_markers': [entry[4] for entry in entries if entry[0] == VENDORS[vendor][-1]],
    }


def generate(directory, sizes):
    directory.mkdir(parents=True, exist_ok=False)
    files = []
    for index, size in enumerate(sizes):
        vendor = tuple(VENDORS)[index % len(VENDORS)]
        suffix = '.dat' if index % 5 == 0 else '.txt'
        path = directory / f'{directory.name}-{index + 1:04d}-{vendor}{suffix}'
        files.append(write_capture(path, size, index + 1, vendor))
        if (index + 1) % 25 == 0 or index + 1 == len(sizes):
            print(f'{directory.name}: {index + 1}/{len(sizes)} captures', flush=True)
    manifest = {
        'format': 1, 'profile': directory.name,
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'total_bytes': sum(sizes), 'files': files,
        'note': 'Synthetic vendor-style text with repeated bounded record windows; not device emulation.',
    }
    with (directory / 'manifest.json').open('x', encoding='utf-8') as output:
        json.dump(manifest, output, indent=2)
        output.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=(*PROFILES, 'full'), default='smoke')
    parser.add_argument('--output', type=Path, default=Path('test-data'))
    parser.add_argument('--budget-gib', type=float, default=12)
    parser.add_argument('--reserve-gib', type=float, default=16)
    parser.add_argument('--plan', action='store_true', help='Show allocation without writing files')
    args = parser.parse_args()
    if not (0 < args.budget_gib <= 20) or not (1 <= args.reserve_gib <= 1024):
        parser.error('Budget must be greater than 0 and at most 20 GiB; reserve must be 1–1024 GiB')
    profiles = ('fleet', 'large', 'mixed') if args.profile == 'full' else (args.profile,)
    target = args.output.resolve()
    # Include existing output under this root in the budget; symlinks consume no capture allocation.
    existing = sum(p.stat().st_size for p in target.rglob('*') if p.is_file() and not p.is_symlink()) if target.exists() else 0
    planned = sum(sum(PROFILES[name]) for name in profiles)
    ancestor = target
    while not ancestor.exists():
        ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    for name in profiles:
        print(f'{name}: {len(PROFILES[name])} files, {sum(PROFILES[name]) / GIB:.3f} GiB')
        if (target / name).exists():
            parser.error(f'{target / name} already exists; existing datasets are never overwritten')
    print(f'New data: {planned / GIB:.3f} GiB; free space after: {(free - planned) / GIB:.1f} GiB')
    if planned + existing + 16 * MIB > args.budget_gib * GIB:
        parser.error('Requested dataset exceeds budget, including existing data and manifest allowance')
    if free - planned - 16 * MIB < args.reserve_gib * GIB:
        parser.error('Insufficient free space to retain the requested reserve')
    if not args.plan:
        for name in profiles:
            generate(target / name, PROFILES[name])


if __name__ == '__main__':
    main()

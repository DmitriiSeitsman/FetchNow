#!/usr/bin/env python3
"""UNKNOWN residual caller attribution for the final candidate images.

Evidence per Debian runtime image (same Image IDs as the Trivy scan):

* package ownership + versions (``/var/lib/dpkg``) of each UNKNOWN family;
* static ELF linkage (DT_NEEDED closure, imported symbols) from product entry
  executables to the family libraries;
* runtime evidence from controlled, self-generated fixtures: libraries
  initialised and symbols bound (glibc ``LD_DEBUG``) for product-shaped argv,
  compared with a control run; Python role modules' loaded objects.

Static linkage is never reported as a runtime call, and a missing chain or a
successful smoke is not reported as unreachability: such rows stay UNKNOWN
with the missing evidence named. Advisory-specific vulnerable-function
reachability is not established here.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import struct
import subprocess
import tarfile
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

FAMILIES: dict[str, dict[str, Any]] = {
    "libxml2": {"packages": ("libxml2",), "priority": 1},
    "libacl": {"packages": ("libacl1",), "priority": 1},
    "ffmpeg-libavformat": {"packages": ("libavformat61",), "priority": 2},
    "cjson": {"packages": ("libcjson1",), "priority": 2},
    "expat": {"packages": ("libexpat1",), "priority": 2},
    "libtiff": {"packages": ("libtiff6",), "priority": 2},
    "libsndfile": {"packages": ("libsndfile1",), "priority": 2},
}
BIND_SONAME_RE = (
    r"libxml2\.so|libexpat\.so|libacl\.so|libcjson\.so|libtiff\.so|libsndfile\.so"
)
INIT_SONAME_RE = BIND_SONAME_RE + r"|libavformat\.so|librist\.so"

DEBIAN_TARGETS: tuple[str, ...] = (
    "api",
    "media-executor",
    "media-net-executor",
    "egress-proxy",
)
DEFAULT_LIB_DIRS: tuple[str, ...] = (
    "/lib/x86_64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu",
    "/lib64",
    "/lib",
    "/usr/lib",
    "/usr/local/lib",
)
PYTHON = "/usr/local/bin/python3.14"
VENV_PYTHON = "/opt/venv/bin/python"
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"
TOOL_USER = "10001:10001"
DUMMY_ENV = (
    "APP_ENV=development",
    "DATABASE_URL=postgresql+asyncpg://audit:audit@127.0.0.1:1/audit",
)

ROLE_MODULES: dict[str, tuple[str, ...]] = {
    "api": ("fetchnow.api.main",),
    "worker": ("fetchnow.worker",),
    "delivery": ("fetchnow.delivery.main",),
    "supervisor": (
        "fetchnow.media_executor.__main__",
        "fetchnow.media_executor.server",
        "fetchnow.media_executor.runner",
        "fetchnow.media_executor.job_process",
    ),
    "proxy": ("fetchnow.media_egress_proxy.server",),
}


@dataclass(frozen=True)
class Role:
    name: str
    entries: tuple[str, ...]
    modules_probe: str | None
    runtime_probes: tuple[str, ...]
    sandbox: str
    privilege: str
    attacker_input: str
    condition: str = "always"


ROLES: dict[str, tuple[Role, ...]] = {
    "api": (
        Role(
            "API",
            (PYTHON,),
            "api",
            (),
            "outside tool sandbox",
            "uid 10001, holds DB/payment secrets",
            "untrusted HTTP requests (JSON, URLs)",
        ),
        Role(
            "secret-bearing worker",
            (PYTHON, FFMPEG, FFPROBE),
            "worker",
            ("ffprobe_mpd", "mux_mpd", "etree_parse"),
            "outside tool sandbox",
            "uid 10001, holds DB secrets",
            "remote provider media bytes and manifests",
            "ffmpeg/ffprobe run in worker when MEDIA_EXECUTOR_ENABLED=false and "
            "MEDIA_MUXING_ENABLED=true; yt-dlp runs in worker when "
            "MEDIA_NET_EXECUTOR_ENABLED=false (defaults false; prior OFF authority "
            "is restored by rollback)",
        ),
        Role(
            "delivery",
            (PYTHON,),
            "delivery",
            (),
            "outside tool sandbox",
            "uid 10001, artifact read access",
            "untrusted HTTP requests; serves artifact bytes without parsing",
        ),
    ),
    "media-executor": (
        Role(
            "root supervisor",
            (PYTHON,),
            "supervisor",
            (),
            "outside tool sandbox",
            "root (uid 0), cgroup/landlock authority",
            "UDS JSON requests from worker",
        ),
        Role(
            "sandboxed media tool",
            (FFMPEG, FFPROBE),
            None,
            ("ffprobe_mpd", "mux_mpd"),
            "inside sec08-launch sandbox (Landlock, network none, cgroup) by "
            "design; sandbox not runtime-verified for these Image IDs here",
            "unprivileged tool process",
            "remote provider media bytes",
            "MEDIA_EXECUTOR_ENABLED=true",
        ),
    ),
    "media-net-executor": (
        Role(
            "root supervisor",
            (PYTHON,),
            "supervisor",
            (),
            "outside tool sandbox",
            "root (uid 0), cgroup/landlock authority",
            "UDS JSON requests from worker",
        ),
        Role(
            "sandboxed media tool",
            (PYTHON,),
            None,
            ("etree_parse",),
            "inside sec08-launch sandbox with egress only via proxy, by design; "
            "sandbox not runtime-verified for these Image IDs here",
            "unprivileged tool process (yt-dlp)",
            "remote provider responses and manifests",
            "MEDIA_NET_EXECUTOR_ENABLED=true",
        ),
    ),
    "egress-proxy": (
        Role(
            "egress proxy",
            (PYTHON,),
            "proxy",
            (),
            "outside tool sandbox (separate container)",
            "uid 65534",
            "CONNECT requests from the sandboxed network tool; DNS answers",
        ),
    ),
}

PROBE_CONTROLS: dict[str, str] = {
    "ffprobe_mpd": "ffprobe_mp4",
    "mux_mpd": "mux_mp4",
    "etree_parse": "etree_import",
}


# --- ELF ------------------------------------------------------------------------

DT_NEEDED, DT_SONAME, DT_RPATH, DT_BIND_NOW, DT_RUNPATH, DT_FLAGS = (
    1,
    14,
    15,
    24,
    29,
    30,
)
DT_FLAGS_1 = 0x6FFFFFFB
SHT_DYNAMIC, SHT_DYNSYM = 6, 11


@dataclass
class ElfInfo:
    needed: list[str] = field(default_factory=list)
    soname: str | None = None
    runpath: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    exports: set[str] = field(default_factory=set)
    bind_now: bool = False
    error: str | None = None


def _cstr(blob: bytes, offset: int) -> str:
    end = blob.find(b"\0", offset)
    return blob[offset : end if end >= 0 else len(blob)].decode("utf-8", "replace")


def parse_elf(data: bytes) -> ElfInfo:
    info = ElfInfo()
    if len(data) < 64 or data[:4] != b"\x7fELF":
        info.error = "not_elf"
        return info
    if data[4] != 2 or data[5] != 1:
        info.error = "not_elf64_le"
        return info
    try:
        shoff = struct.unpack_from("<Q", data, 0x28)[0]
        shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
        if not shoff or not shnum or shentsize != 64:
            info.error = "no_section_headers"
            return info
        sections = [
            struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * 64)
            for i in range(shnum)
        ]

        def body(index: int) -> bytes:
            sec = sections[index]
            return data[sec[4] : sec[4] + sec[5]]

        for sec in sections:
            sh_type, sh_offset, sh_size, sh_link = sec[1], sec[4], sec[5], sec[6]
            if sh_type == SHT_DYNAMIC:
                strtab = body(sh_link)
                for off in range(sh_offset, sh_offset + sh_size - 15, 16):
                    tag, val = struct.unpack_from("<qQ", data, off)
                    if tag == 0:
                        break
                    if tag == DT_NEEDED:
                        info.needed.append(_cstr(strtab, val))
                    elif tag == DT_SONAME:
                        info.soname = _cstr(strtab, val)
                    elif tag in (DT_RPATH, DT_RUNPATH):
                        info.runpath.extend(
                            p for p in _cstr(strtab, val).split(":") if p
                        )
                    elif (
                        tag == DT_BIND_NOW
                        or tag == DT_FLAGS
                        and val & 0x8
                        or tag == DT_FLAGS_1
                        and val & 0x1
                    ):
                        info.bind_now = True
            elif sh_type == SHT_DYNSYM:
                strtab = body(sh_link)
                for off in range(sh_offset + 24, sh_offset + sh_size - 23, 24):
                    st_name, st_info, _other, st_shndx = struct.unpack_from(
                        "<IBBH", data, off
                    )
                    if not st_name:
                        continue
                    name = _cstr(strtab, st_name)
                    if st_shndx == 0:
                        info.imports.add(name)
                    elif st_info >> 4 in (1, 2):
                        info.exports.add(name)
    except (struct.error, IndexError):
        info.error = "malformed"
    return info


# --- image filesystem ----------------------------------------------------------------


@dataclass
class ImageFs:
    elves: dict[str, ElfInfo] = field(default_factory=dict)
    symlinks: dict[str, str] = field(default_factory=dict)
    dpkg_status: str = ""
    dpkg_lists: dict[str, list[str]] = field(default_factory=dict)
    ytdlp_etree_users: list[str] = field(default_factory=list)


def _abs(name: str) -> str:
    return posixpath.normpath("/" + name.lstrip("./"))


def scan_tar(stream: IO[bytes]) -> ImageFs:
    fs = ImageFs()
    with tarfile.open(fileobj=stream, mode="r|") as tf:
        for member in tf:
            path = _abs(member.name)
            if member.issym():
                fs.symlinks[path] = member.linkname
                continue
            if member.islnk():
                fs.symlinks[path] = "/" + member.linkname.lstrip("./")
                continue
            if not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is None:
                continue
            if path == "/var/lib/dpkg/status":
                fs.dpkg_status = handle.read().decode("utf-8", "replace")
            elif path.startswith("/var/lib/dpkg/info/") and path.endswith(".list"):
                pkg = posixpath.basename(path)[: -len(".list")].split(":")[0]
                text = handle.read().decode("utf-8", "replace")
                fs.dpkg_lists[pkg] = [ln for ln in text.splitlines() if ln.strip()]
            elif "/site-packages/yt_dlp/" in path and path.endswith(".py"):
                text = handle.read().decode("utf-8", "replace")
                if "xml.etree" in text or "compat_etree_fromstring" in text:
                    fs.ytdlp_etree_users.append(path)
            elif member.size >= 64:
                head = handle.read(4)
                if head == b"\x7fELF":
                    fs.elves[path] = parse_elf(head + handle.read())
    return fs


def resolve_path(fs: ImageFs, path: str, *, max_hops: int = 40) -> str | None:
    parts = [p for p in posixpath.normpath(path).split("/") if p]
    current = "/"
    hops = 0
    while parts:
        candidate = posixpath.join(current, parts.pop(0))
        if candidate in fs.symlinks:
            hops += 1
            if hops > max_hops:
                return None
            target = fs.symlinks[candidate]
            base = "/" if target.startswith("/") else current
            parts = [
                p
                for p in posixpath.normpath(posixpath.join(base, target)).split("/")
                if p
            ] + parts
            current = "/"
            continue
        current = candidate
    return current


def parse_dpkg_status(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        if fields.get("Package") and "installed" in fields.get("Status", ""):
            out[fields["Package"]] = fields.get("Version", "")
    return out


def owned_elves(fs: ImageFs, package: str) -> list[str]:
    out = set()
    for listed in fs.dpkg_lists.get(package, []):
        real = resolve_path(fs, listed)
        if real and real in fs.elves:
            out.add(real)
    return sorted(out)


def resolve_needed(fs: ImageFs, obj: str, name: str) -> str | None:
    info = fs.elves.get(obj)
    dirs: list[str] = []
    if info:
        origin = posixpath.dirname(obj)
        dirs += [
            d.replace("$ORIGIN", origin).replace("${ORIGIN}", origin)
            for d in info.runpath
        ]
    for directory in [*dirs, *DEFAULT_LIB_DIRS]:
        real = resolve_path(fs, posixpath.join(directory, name))
        if real and real in fs.elves:
            return real
    return None


def closure_chains(
    fs: ImageFs, entry: str, targets: Iterable[str]
) -> dict[str, list[str]]:
    start = resolve_path(fs, entry)
    wanted = set(targets)
    if not start or start not in fs.elves:
        return {}
    parent: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for name in fs.elves[node].needed:
            dep = resolve_needed(fs, node, name)
            if dep and dep not in parent:
                parent[dep] = node
                queue.append(dep)
    chains: dict[str, list[str]] = {}
    for lib in wanted & set(parent):
        chain: list[str] = []
        cur: str | None = lib
        while cur is not None:
            chain.append(cur)
            cur = parent[cur]
        chains[lib] = list(reversed(chain))
    return chains


def direct_consumers(fs: ImageFs, libs: Sequence[str]) -> list[dict[str, Any]]:
    lib_set = set(libs)
    out: list[dict[str, Any]] = []
    for path, info in sorted(fs.elves.items()):
        for name in info.needed:
            dep = resolve_needed(fs, path, name)
            if dep in lib_set:
                exports = fs.elves[dep].exports
                imported = sorted(info.imports & exports)
                out.append(
                    {
                        "consumer": path,
                        "library": dep,
                        "consumer_bind_now": info.bind_now,
                        "imported_symbol_count": len(imported),
                        "imported_symbols_sample": imported[:12],
                    }
                )
    return out


# --- runtime probes ----------------------------------------------------------------

MPD_FIXTURE = (
    '<?xml version="1.0"?>\n'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
    'mediaPresentationDuration="PT0.2S" minBufferTime="PT1S" '
    'profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">'
    '<Period><AdaptationSet mimeType="video/mp4">'
    '<Representation id="1" bandwidth="1000" codecs="avc1.42c00a" width="64" height="48">'
    "<BaseURL>v.mp4</BaseURL></Representation></AdaptationSet></Period></MPD>\n"
)
FFPROBE_ARGS = (
    "-hide_banner -loglevel error -protocol_whitelist file -print_format json "
    "-show_entries stream=codec_type,codec_name:format=format_name,duration,size -i"
)
MUX_ARGS_TAIL = (
    "-protocol_whitelist file -i /tmp/fnp/a.m4a -map 0:v:0 -map 1:a:0 -c copy -sn -dn "
    "-map_metadata -1 -map_metadata:s -1 -map_chapters -1 -f mp4"
)


def _ld_dump_script() -> str:
    return (
        'for f in /tmp/fnp/ld/*; do [ -f "$f" ] || continue; '
        'b=$(basename "$f"); l=${b%.*}; '
        f'grep -E \'calling init: .*({INIT_SONAME_RE})\' "$f" | sed "s|^ *[0-9]*:[[:space:]]*|$l\\t|"; '
        f'grep -E \'binding file .* to [^ ]*({BIND_SONAME_RE})[^ ]* \' "$f" | sed "s|^ *[0-9]*:[[:space:]]*|$l\\t|"; '
        "done | sort -u | head -n 4000"
    )


def tool_probe_script() -> str:
    run = (
        "run() { l=$1; shift; LD_DEBUG=libs,bindings LD_DEBUG_OUTPUT=/tmp/fnp/ld/$l "
        '"$@" >/tmp/fnp/$l.out 2>/tmp/fnp/$l.err; echo "__RC__ $l $?"; }\n'
    )
    return (
        "set -u\nmkdir -p /tmp/fnp/ld && cd /tmp/fnp\n"
        f"{FFMPEG} -hide_banner -loglevel error -y -f lavfi -i color=c=black:s=64x48:d=0.2 "
        "-c:v libx264 -pix_fmt yuv420p v.mp4\n"
        f"{FFMPEG} -hide_banner -loglevel error -y -f lavfi -i anullsrc=r=48000:cl=stereo "
        "-t 0.2 -c:a aac a.m4a\n"
        f"cat > m.mpd <<'EOF'\n{MPD_FIXTURE}EOF\n"
        + run
        + f"run ffprobe_mp4 {FFPROBE} {FFPROBE_ARGS} /tmp/fnp/v.mp4\n"
        f"run ffprobe_mpd {FFPROBE} {FFPROBE_ARGS} /tmp/fnp/m.mpd\n"
        f"run mux_mp4 {FFMPEG} -hide_banner -nostdin -nostats -loglevel error -n "
        f"-protocol_whitelist file -i /tmp/fnp/v.mp4 {MUX_ARGS_TAIL} /tmp/fnp/o1.mp4\n"
        f"run mux_mpd {FFMPEG} -hide_banner -nostdin -nostats -loglevel error -n "
        f"-protocol_whitelist file -i /tmp/fnp/m.mpd {MUX_ARGS_TAIL} /tmp/fnp/o2.mp4\n"
        "echo __FORMAT_MP4__; tr -d '\\n ' < ffprobe_mp4.out; echo\n"
        "echo __FORMAT_MPD__; tr -d '\\n ' < ffprobe_mpd.out; echo\n"
        f"echo __DEMUXERS__; {FFMPEG} -hide_banner -demuxers 2>/dev/null | "
        "grep -E ' (dash|asf|rtp|rtsp|sdp|hls|mpegps)( |$)'\n"
        f"echo __PROTOCOLS__; {FFMPEG} -hide_banner -protocols 2>/dev/null | tr -s ' \\n' ' '; echo\n"
        "echo __LD__\n" + _ld_dump_script() + "\n"
    )


def etree_probe_script(python: str) -> str:
    parse = (
        "try:\n from yt_dlp.compat import compat_etree_fromstring as f; src='yt_dlp.compat'\n"
        "except ImportError:\n from xml.etree.ElementTree import fromstring as f; src='stdlib'\n"
        "f('<MPD xmlns=\"urn:mpeg:dash:schema:mpd:2011\"><Period/></MPD>'); print('parser=' + src)\n"
    )
    return (
        "set -u\nmkdir -p /tmp/fnp/ld && cd /tmp/fnp\n"
        f"cat > parse.py <<'EOF'\n{parse}EOF\n"
        "run() { l=$1; shift; LD_DEBUG=libs,bindings LD_DEBUG_OUTPUT=/tmp/fnp/ld/$l "
        '"$@" >/tmp/fnp/$l.out 2>/tmp/fnp/$l.err; echo "__RC__ $l $?"; }\n'
        f"run etree_import {python} -c 'import yt_dlp'\n"
        f"run etree_parse {python} /tmp/fnp/parse.py\n"
        "echo __PARSER__; cat etree_parse.out\n"
        "echo __LD__\n" + _ld_dump_script() + "\n"
    )


def modules_probe_code(modules: Sequence[str]) -> str:
    return (
        "import importlib, json\nerrors = {}\n"
        f"for m in {list(modules)!r}:\n"
        "    try:\n        importlib.import_module(m)\n"
        "    except Exception as exc:\n        errors[m] = type(exc).__name__ + ': ' + str(exc)[:200]\n"
        "libs = sorted({ln.split()[-1] for ln in open('/proc/self/maps') "
        "if len(ln.split()) >= 6 and '.so' in ln.split()[-1]})\n"
        "print('__MAPS__' + json.dumps({'errors': errors, 'libs': libs}))\n"
    )


BIND_RE = re.compile(
    r"^(?P<label>[\w.-]+)\tbinding file (?P<src>\S+) \[\d+\] to (?P<dst>\S+) \[\d+\]: "
    r"normal symbol `(?P<sym>[^']+)'"
)
INIT_RE = re.compile(r"^(?P<label>[\w.-]+)\tcalling init: (?P<lib>\S+)")


def parse_ld_lines(text: str) -> dict[str, dict[str, set[Any]]]:
    out: dict[str, dict[str, set[Any]]] = {}
    for line in text.splitlines():
        match = BIND_RE.match(line)
        if match:
            row = out.setdefault(match["label"], {"init": set(), "bind": set()})
            row["bind"].add((match["src"], match["dst"], match["sym"]))
            continue
        match = INIT_RE.match(line)
        if match:
            out.setdefault(match["label"], {"init": set(), "bind": set()})["init"].add(
                match["lib"]
            )
    return out


def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"_": []}
    current = "_"
    for line in text.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("__")
            and stripped.endswith("__")
            and " " not in stripped
        ):
            current = stripped.strip("_")
            sections.setdefault(current, [])
            continue
        sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def call_specific(
    ld: Mapping[str, Mapping[str, set[Any]]],
    trial: str,
    control: str,
    libs: Iterable[str],
) -> list[tuple[str, str, str]]:
    lib_names = {posixpath.basename(lib) for lib in libs}

    def matches(dst: str) -> bool:
        base = posixpath.basename(dst)
        return any(
            base == n or base.startswith(n.split(".so")[0] + ".so") for n in lib_names
        )

    trial_b = {b for b in ld.get(trial, {}).get("bind", set()) if matches(b[1])}
    control_b = {b for b in ld.get(control, {}).get("bind", set()) if matches(b[1])}
    return sorted(trial_b - control_b)


def loaded(
    ld: Mapping[str, Mapping[str, set[Any]]], label: str, libs: Iterable[str]
) -> bool:
    stems = {posixpath.basename(lib).split(".so")[0] for lib in libs}
    return any(
        posixpath.basename(lib).split(".so")[0] in stems
        for lib in ld.get(label, {}).get("init", set())
    )


# --- verdicts --------------------------------------------------------------------------


def classify(
    *,
    family: str,
    libs: Sequence[str],
    role: Role,
    chains: Mapping[str, Any],
    ld: Mapping[str, Mapping[str, set[Any]]],
    maps_libs: Sequence[str],
    demux_dash: bool,
) -> dict[str, Any]:
    evidence: list[str] = []
    missing: list[str] = []
    confirmed_calls: list[tuple[str, str, str]] = []
    runtime_loaded = False
    for probe in role.runtime_probes:
        control = PROBE_CONTROLS[probe]
        if family == "ffmpeg-libavformat":
            if probe in ("ffprobe_mpd", "mux_mpd") and demux_dash:
                evidence.append(
                    f"{probe}: content probe selected dash demuxer (format_name=dash)"
                )
                runtime_loaded = True
            continue
        calls = call_specific(ld, probe, control, libs)
        if calls:
            confirmed_calls.extend(calls)
            evidence.append(
                f"{probe}: {len(calls)} symbol bindings absent in {control}"
            )
        if loaded(ld, probe, libs):
            runtime_loaded = True
            evidence.append(f"{probe}: library initialised")
    stems = {posixpath.basename(lib).split(".so")[0] for lib in libs}
    if any(posixpath.basename(m).split(".so")[0] in stems for m in maps_libs):
        runtime_loaded = True
        evidence.append("role modules: library mapped after import")
    if chains:
        evidence.append("static DT_NEEDED chain from role entry")
    dash_parse = (
        family == "libxml2"
        and demux_dash
        and runtime_loaded
        and any(p in ("ffprobe_mpd", "mux_mpd") for p in role.runtime_probes)
    )
    if dash_parse:
        evidence.append(
            "runtime MPD parse by dash demuxer (stream resolved from BaseURL); "
            "FFmpeg dashdec parses MPD only through libxml2 — inferred call, "
            "symbol bindings not separable from load when consumers are BIND_NOW"
        )
    if (
        confirmed_calls
        or dash_parse
        or (family == "ffmpeg-libavformat" and runtime_loaded and demux_dash)
    ):
        verdict = "confirmed"
        if dash_parse and not confirmed_calls:
            missing.append("symbol-level call trace (consumer BIND_NOW)")
        missing.append("advisory-specific vulnerable function not traced")
    elif chains or runtime_loaded:
        verdict = "potentially reachable"
        if chains and not runtime_loaded:
            missing.append("runtime load/call not observed (static linkage only)")
        missing.append("product call into library with attacker input not traced")
        missing.append("advisory-specific vulnerable function not traced")
    else:
        verdict = "UNKNOWN"
        missing.append("no static chain from known product entries found")
        missing.append(
            "exec of other binaries (healthchecks, shells, operator exec) not traced; "
            "absence of a chain is not proof of unreachability"
        )
    return {
        "family": family,
        "role": role.name,
        "condition": role.condition,
        "sandbox": role.sandbox,
        "privilege": role.privilege,
        "attacker_input": role.attacker_input,
        "verdict": verdict,
        "evidence": evidence,
        "confirmed_call_bindings_sample": [list(c) for c in confirmed_calls[:10]],
        "static_chains": chains,
        "missing_evidence": missing,
    }


# --- docker collection ----------------------------------------------------------------


def _docker(
    argv: Sequence[str], timeout: float = 300.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )


def image_id(ref: str) -> str:
    proc = _docker(["image", "inspect", "--format", "{{.Id}}", ref], timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def export_fs(ref: str) -> ImageFs:
    created = _docker(["create", "--network", "none", ref, "true"], timeout=60)
    if created.returncode != 0:
        raise RuntimeError(f"create failed: {created.stderr[-300:]}")
    cid = created.stdout.strip()
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        fs = scan_tar(proc.stdout)
        if proc.wait(timeout=600) != 0:
            raise RuntimeError("docker export failed")
        return fs
    finally:
        _docker(["rm", "-f", cid], timeout=60)


def run_shell(ref: str, script: str, *, user: str | None) -> tuple[int, str]:
    argv = ["run", "--rm", "--network", "none"]
    if user:
        argv += ["--user", user]
    argv += ["--entrypoint", "/bin/sh", ref, "-c", script]
    proc = _docker(argv, timeout=300)
    return proc.returncode, proc.stdout + "\n" + proc.stderr[-2000:]


def run_modules(ref: str, modules: Sequence[str]) -> dict[str, Any]:
    argv = ["run", "--rm", "--network", "none"]
    for env in DUMMY_ENV:
        argv += ["-e", env]
    argv += ["--entrypoint", "python", ref, "-c", modules_probe_code(modules)]
    proc = _docker(argv, timeout=120)
    for line in proc.stdout.splitlines():
        if line.startswith("__MAPS__"):
            data: dict[str, Any] = json.loads(line[len("__MAPS__") :])
            data["returncode"] = proc.returncode
            return data
    return {
        "returncode": proc.returncode,
        "errors": {"probe": proc.stderr[-500:]},
        "libs": [],
    }


def attribute_target(target: str, ref: str) -> dict[str, Any]:
    before = image_id(ref)
    fs = export_fs(ref)
    versions = parse_dpkg_status(fs.dpkg_status)
    family_libs = {
        fam: [lib for pkg in spec["packages"] for lib in owned_elves(fs, pkg)]
        for fam, spec in FAMILIES.items()
    }
    probe_text: dict[str, str] = {}
    roles = ROLES[target]
    if any(p in ("ffprobe_mpd", "mux_mpd") for r in roles for p in r.runtime_probes):
        _rc, probe_text["tools"] = run_shell(ref, tool_probe_script(), user=TOOL_USER)
    if any("etree_parse" in r.runtime_probes for r in roles):
        user = TOOL_USER if target == "api" else "65534:65534"
        _rc, probe_text["etree"] = run_shell(
            ref, etree_probe_script(VENV_PYTHON), user=user
        )
    ld: dict[str, dict[str, set[Any]]] = {}
    sections: dict[str, dict[str, str]] = {}
    for key, text in probe_text.items():
        sections[key] = split_sections(text)
        for label, row in parse_ld_lines(sections[key].get("LD", "")).items():
            ld.setdefault(label, {"init": set(), "bind": set()})
            ld[label]["init"] |= row["init"]
            ld[label]["bind"] |= row["bind"]
    tools = sections.get("tools", {})
    mpd_probe = tools.get("FORMAT_MPD", "")
    demux_dash = (
        '"format_name":"dash"' in mpd_probe and '"codec_type":"video"' in mpd_probe
    )
    modules: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for role in roles:
        maps: dict[str, Any] = {"libs": []}
        if role.modules_probe:
            maps = run_modules(ref, ROLE_MODULES[role.modules_probe])
            modules[role.name] = maps
        for fam, libs in family_libs.items():
            if not libs:
                continue
            chains: dict[str, Any] = {}
            for entry in role.entries:
                found = closure_chains(fs, entry, libs)
                if found:
                    chains[entry] = found
            rows.append(
                classify(
                    family=fam,
                    libs=libs,
                    role=role,
                    chains=chains,
                    ld=ld,
                    maps_libs=maps.get("libs", []),
                    demux_dash=demux_dash,
                )
            )
    after = image_id(ref)
    return {
        "target": target,
        "reference": ref,
        "image_id_before": before,
        "image_id_after": after,
        "image_id_stable": bool(before) and before == after,
        "family_packages": {
            fam: {pkg: versions.get(pkg) for pkg in spec["packages"]}
            for fam, spec in FAMILIES.items()
        },
        "family_libraries": family_libs,
        "family_direct_consumers": {
            fam: direct_consumers(fs, libs) for fam, libs in family_libs.items() if libs
        },
        "bind_now_entries": {
            e: fs.elves[r].bind_now
            for e in (FFMPEG, FFPROBE, PYTHON)
            if (r := resolve_path(fs, e)) and r in fs.elves
        },
        "ytdlp_etree_users": sorted(fs.ytdlp_etree_users),
        "probe_sections": {
            key: {k: v[-6000:] for k, v in sec.items() if k != "LD"}
            for key, sec in sections.items()
        },
        "ld_evidence": {
            label: {"init": sorted(row["init"]), "bind_count": len(row["bind"])}
            for label, row in sorted(ld.items())
        },
        "role_modules": modules,
        "verdicts": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UNKNOWN caller attribution")
    parser.add_argument("--targets-file", type=Path, required=True)
    parser.add_argument("--identities-file", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    targets = json.loads(args.targets_file.read_text(encoding="utf-8"))
    identities = json.loads(args.identities_file.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for target in DEBIAN_TARGETS:
        try:
            results[target] = attribute_target(target, targets[target])
        except Exception as exc:  # noqa: BLE001 - recorded as technical evidence gap
            errors[target] = f"{type(exc).__name__}: {exc}"[:500]
    identity_ok = all(
        results.get(t, {}).get("image_id_before") == identities[t]["image_id"]
        and results.get(t, {}).get("image_id_stable")
        for t in DEBIAN_TARGETS
    )
    payload = {
        "ok": not errors and identity_ok,
        "identity_matches_scanned_images": identity_ok,
        "errors": errors,
        "alpine_targets_note": (
            "web, gateway, postgres carry no UNKNOWN-family packages in the "
            "historical residual set; no attribution rows produced"
        ),
        "targets": results,
    }
    args.json_out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": payload["ok"], "errors": errors}, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

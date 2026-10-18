#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wansime.py - WAN simulator for Linux / Raspberry Pi (tc netem) with TUI and web GUI.
by Ewald Jeitler - https://www.jeitler.cc

Modes
  bridge  transparent L2 bridge between two ports, impairments per direction (A->B, B->A)
  single  one interface, impairs local traffic: OUT = egress, IN = ingress via IFB device
"""
import sys

MIN_PYTHON = (3, 8)
if sys.version_info < MIN_PYTHON:
    sys.exit("wansime.py requires Python %d.%d or newer (found %d.%d)" % (MIN_PYTHON + sys.version_info[:2]))

import argparse
import base64
import collections
import contextlib
import locale
import copy
import curses
import hmac
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "v1.63"
REORDER_MIN_DELAY = 10  # ms; netem reorder only works on top of a delay
PROG = "wansime.py"
CREDIT = "by Ewald Jeitler - https://www.jeitler.cc"
THX = f"THX for using {PROG} {VERSION} - www.jeitler.cc"
WEB_PORT = 8082
CFG_DIR = "/etc/wansim"
CFG_FILE = f"{CFG_DIR}/config.json"
STATE_DIR = "/var/lib/wansim"
ORIG_FILE = f"{STATE_DIR}/original.json"
MODLOAD_FILE = "/etc/modules-load.d/wansim.conf"
UNIT_FILE = "/etc/systemd/system/wansime.service"
INSTALL_PATH = "/usr/local/sbin/wansime.py"
IFB = "ifbwsim"
SCRIPT_NAMES = ("wansime.py", "wansim.py")  # current and former file name

os.environ["PATH"] = os.environ.get("PATH", "") + ":/usr/local/sbin:/usr/sbin:/sbin"


class SimError(Exception):
    pass


class Warn(Exception):
    pass


# ----------------------------------------------------------------------------- parameters
# key, label, unit, step, min, max
PARAMS = [
    ("delay", "Delay", "ms", 5, 0, 60000),
    ("jitter", "Jitter", "ms", 1, 0, 60000),
    ("loss", "Loss", "%", 0.1, 0, 100),
    ("burst", "Consecutive packet loss", "avg. packets per burst, 1 = off", 1, 1, 1000),
    ("reorder", "Reorder (sets min. delay)", "%", 0.5, 0, 100),
    ("dup", "Duplicate", "%", 0.1, 0, 100),
    ("corrupt", "Corrupt", "%", 0.1, 0, 100),
    ("rate", "Rate limit", "kbit/s, 0 = off", 1000, 0, 100000000),
    ("mtu", "MTU (max. frame size)", "bytes, default = port MTU", 100, 0, 9216),
]
# slider range per parameter: min, max, step (values outside the range can still be typed)
SLIDER = {"delay": (0, 1000, 1), "jitter": (0, 500, 1), "loss": (0, 20, 0.1), "burst": (1, 50, 1),
          "reorder": (0, 50, 0.1), "dup": (0, 20, 0.1), "corrupt": (0, 20, 0.1), "rate": (0, 100000, 100),
          "mtu": (576, 9100, 1)}
DEF_DIR = {"delay": 0.0, "jitter": 0.0, "loss": 0.0, "burst": 1, "reorder": 0.0, "dup": 0.0,
           "corrupt": 0.0, "rate": 0, "mtu": 0}
DEF_CFG = {
    "mode": "bridge",
    "bridge": {"name": "br0", "port_a": "", "port_b": "", "lldp": True, "offloads_off": True},
    "single": {"iface": ""},
    "a2b": dict(DEF_DIR),
    "b2a": dict(DEF_DIR),
}


def _d(**kw):
    return {**DEF_DIR, **kw}


# direction A->B = upstream / OUT, B->A = downstream / IN
# delay / jitter are one-way values per direction: RTT = A->B delay + B->A delay; rate in kbit/s
PRESETS = {
    "Baseline: no impairment": (_d(), _d()),
    "RTT 20 ms, jitter 1 ms, loss 0.01 %": (_d(delay=10, jitter=1, loss=0.01), _d(delay=10, jitter=1, loss=0.01)),
    "RTT 100 ms, jitter 3 ms, loss 0.1 %": (_d(delay=50, jitter=3, loss=0.1), _d(delay=50, jitter=3, loss=0.1)),
    "RTT 300 ms, jitter 8 ms, loss 0.1 %": (_d(delay=150, jitter=8, loss=0.1), _d(delay=150, jitter=8, loss=0.1)),
    "RTT 30 ms, jitter 2 ms, 1M up / 16M down": (_d(delay=15, jitter=2, rate=1000), _d(delay=15, jitter=2, rate=16000)),
    "RTT 70 ms, jitter 10 ms, loss 0.3 %, 10M up / 40M down": (_d(delay=35, jitter=10, loss=0.3, rate=10000),
                                                              _d(delay=35, jitter=10, loss=0.3, rate=40000)),
    "RTT 600 ms, jitter 10 ms, loss 0.5 %, 5M up / 20M down": (_d(delay=300, jitter=10, loss=0.5, rate=5000),
                                                              _d(delay=300, jitter=10, loss=0.5, rate=20000)),
    "RTT 80 ms, jitter 30 ms, loss 2 %, reorder 1 %, 10M/10M": (_d(delay=40, jitter=30, loss=2, reorder=1, rate=10000),
                                                                _d(delay=40, jitter=30, loss=2, reorder=1, rate=10000)),
}


def clean_dir(d):
    out = {}
    for k, label, unit, _step, lo, hi in PARAMS:
        v = d.get(k, DEF_DIR[k])
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise SimError(f"{label}: not a number")
        if not lo <= v <= hi:
            raise SimError(f"{label}: allowed range {lo:g} .. {hi:g}")
        out[k] = int(v) if k in ("rate", "burst", "mtu") else round(v, 4)
    if 0 < out["mtu"] < 576:
        out["mtu"] = 576
    if out["jitter"] > 0 and out["delay"] <= 0:
        out["delay"] = out["jitter"]  # jitter needs a base delay (tc: "distribution specified but no latency and jitter")
    if out["reorder"] > 0 and out["delay"] < REORDER_MIN_DELAY:
        out["delay"] = REORDER_MIN_DELAY  # reorder needs a delay: raise it automatically
    return out


def merge_cfg(base, new):
    c = copy.deepcopy(base)
    if new.get("mode") in ("bridge", "single"):
        c["mode"] = new["mode"]
    elif "mode" in new:
        raise SimError("mode must be 'bridge' or 'single'")
    nm = r"[A-Za-z][A-Za-z0-9_.\-]{0,14}"
    if "bridge" in new:
        b = {**c["bridge"], **new["bridge"]}
        if not re.fullmatch(nm, str(b["name"])):
            raise SimError("Bridge name: letters/digits/_.- , max 15 characters")
        for k in ("port_a", "port_b"):
            b[k] = str(b[k] or "")
            if b[k] and not re.fullmatch(nm, b[k]):
                raise SimError(f"Invalid interface name: {b[k]}")
        b.pop("mtu", None)  # moved to the WAN parameters (per direction)
        b["lldp"] = bool(b["lldp"])
        b["offloads_off"] = bool(b.get("offloads_off", True))
        c["bridge"] = b
    if "single" in new:
        s = str(new["single"].get("iface", "") or "")
        if s and not re.fullmatch(nm, s):
            raise SimError(f"Invalid interface name: {s}")
        c["single"] = {"iface": s}
    for d in ("a2b", "b2a"):
        if d in new:
            c[d] = clean_dir({**c[d], **new[d]})
    return c


# ----------------------------------------------------------------------------- helpers
def run(cmd, timeout=15, env=None):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                             start_new_session=True)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err.strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=2)
        except Exception:
            pass  # stuck in the kernel (e.g. NIC resetting): give up on it, do not wait
        return 124, "", f"{' '.join(cmd[:4])}: no answer after {timeout}s (NIC/kernel busy?)"



def must(cmd, **kw):
    rc, out, err = run(cmd, **kw)
    if rc:
        raise SimError(f"{' '.join(cmd)}: {err or out.strip() or 'rc=%d' % rc}")
    return out


def read_text(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=0):
    try:
        return int(read_text(path, str(default)))
    except ValueError:
        return default


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ip_json(*args):
    rc, out, _ = run(["ip", "-j", *args])
    try:
        return json.loads(out) if rc == 0 and out.strip() else []
    except ValueError:
        return []


def parse_rate(s):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)(?:bit(?:/s)?)?\s*", s)
    if not m:
        raise SimError("Rate: e.g. 512k, 20m, 1g (plain number = kbit, 0 = off)")
    return int(float(m.group(1)) * {"": 1, "k": 1, "m": 1000, "g": 1000000}[m.group(2).lower()])


def fmt_kbit(k):
    if not k:
        return "off"
    if k >= 1000000:
        return f"{k / 1000000:g} Gbit"
    if k >= 1000:
        return f"{k / 1000:g} Mbit"
    return f"{k} kbit"


def fmt_bps(b):
    for lim, u in ((1e9, "Gbit/s"), (1e6, "Mbit/s"), (1e3, "kbit/s")):
        if b >= lim:
            return f"{b / lim:.2f} {u}"
    return f"{b:.0f} bit/s"


def fmt_bytes(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} TiB"


def nice_scale(v):
    """round a graph maximum up to 1/2/5 x 10^n (at least 1 kbit/s)"""
    v = max(float(v), 1000.0)
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * e:
            return m * e


_BRL = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))  # braille dots of the left / right column, top to bottom


def graph_rows(ins, outs, width, height, scale, utf=True):
    """btop-like braille area graph. ins/outs: bit/s samples (newest last). Returns `height` rows (top first) of
    (char, passed) tuples; passed = the cell contains OUT (green), otherwise only IN (white)."""
    n = 2 * width
    ins = ([0.0] * n + list(ins))[-n:]
    outs = ([0.0] * n + list(outs))[-n:]
    dots = 4 * height
    hi = [min(dots, round(min(v, scale) / scale * dots)) for v in ins]
    ho = [min(dots, round(min(v, scale) / scale * dots)) for v in outs]
    rows = []
    for ry in range(height):
        cy = height - 1 - ry  # character row counted from the bottom
        row = []
        for cx in range(width):
            bits, passed = 0, False
            for sub in (0, 1):
                i = 2 * cx + sub
                fill = max(0, min(4, max(hi[i], ho[i]) - cy * 4))
                if max(0, min(4, ho[i] - cy * 4)):
                    passed = True
                for k in range(fill):  # fill from the bottom of the character upwards
                    bits |= _BRL[sub][3 - k]
            if not bits:
                row.append((" ", False))
            elif utf:
                row.append((chr(0x2800 + bits), passed))
            else:
                row.append(("#" if passed else ".", passed))
        rows.append(row)
    return rows


def fmt_val(key, v):
    if key == "rate":
        return fmt_kbit(v)
    if key == "burst":
        return "off" if v <= 1 else f"{v} pkts"
    if key == "mtu":
        return f"{v:g} B"
    return f"{v:g} " + ("ms" if key in ("delay", "jitter") else "%")


def local_ips():
    res = []
    for d in ip_json("addr", "show"):
        for a in d.get("addr_info", []):
            if a.get("family") == "inet" and not a["local"].startswith("127."):
                res.append(a["local"])
    return res[:3]


# ----------------------------------------------------------------------------- interfaces
def is_candidate(n, show_all):
    base = f"/sys/class/net/{n}"
    if n == "lo" or n == IFB or n.startswith("ifb"):
        return False
    if any(os.path.exists(f"{base}/{x}") for x in ("bridge", "wireless", "phy80211")):
        return False
    if read_text(f"{base}/type") != "1":
        return False
    return show_all or os.path.exists(f"{base}/device")


def iface_info(show_all=False):
    out = []
    for d in ip_json("addr", "show"):
        n = d["ifname"]
        if not is_candidate(n, show_all):
            continue
        out.append({
            "name": n, "state": d.get("operstate", "?").lower(), "mtu": d.get("mtu"),
            "master": d.get("master", ""), "mac": d.get("address", ""),
            "addrs": [f'{a["local"]}/{a["prefixlen"]}' for a in d.get("addr_info", [])
                      if a.get("family") in ("inet", "inet6") and a.get("scope") != "link"],
            "speed": read_int(f"/sys/class/net/{n}/speed", -1),
        })
    return out


def counters(n):
    b = f"/sys/class/net/{n}/statistics/"
    return {k: read_int(b + k) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")}


def netem_stats(n):
    rc, out, _ = run(["tc", "-j", "-s", "qdisc", "show", "dev", n])
    try:
        for q in json.loads(out):
            if q.get("kind") == "netem":
                return {"packets": q.get("packets", 0), "bytes": q.get("bytes", 0),
                        "drops": q.get("drops", 0), "backlog": q.get("backlog", 0),
                        "opts": json.dumps(q.get("options", {}), separators=(",", ":"))}
    except ValueError:
        pass
    return None


def bridge_active(name):
    return bool(name) and os.path.isdir(f"/sys/class/net/{name}/bridge")


def default_mtu(c, ifaces):
    """default (= unchanged) MTU shown for [A->B / OUT, B->A / IN]"""
    by = {i["name"]: i.get("mtu") for i in ifaces}
    if c["mode"] == "single":
        dev = c["single"]["iface"]
        snap = load_json(ORIG_FILE) or {}
        return [(snap.get("mtu") if snap.get("mode") == "single" else 0) or by.get(dev) or 1500] * 2
    b = c["bridge"]
    snap = load_json(ORIG_FILE) or {}
    sp = snap.get("ports", {}) if snap.get("mode") == "bridge" else {}
    return [sp.get(b["port_b"], {}).get("mtu") or by.get(b["port_b"]) or 1500,
            sp.get(b["port_a"], {}).get("mtu") or by.get(b["port_a"]) or 1500]


def snap_alive(snap):
    if not snap:
        return False
    if snap.get("mode") == "single":
        return os.path.isdir(f"/sys/class/net/{snap.get('ifb', IFB)}")
    return bridge_active(snap.get("bridge", ""))


def wan_devs(c):
    """(device carrying A->B / OUT, device carrying B->A / IN)"""
    if c["mode"] == "single":
        return c["single"]["iface"], IFB
    return c["bridge"]["port_b"], c["bridge"]["port_a"]


def dir_labels(c):
    if c["mode"] == "single":
        d = c["single"]["iface"] or "IF"
        return f"OUT (egress {d})", f"IN (ingress {d} via IFB)"
    pa, pb = c["bridge"]["port_a"] or "A", c["bridge"]["port_b"] or "B"
    return f"{pa} -> {pb}", f"{pb} -> {pa}"


def mgmt_risk(ports, client=None):
    risks = []
    for r in ip_json("route", "show", "default"):
        if r.get("dev") in ports:
            risks.append(f"{r['dev']} carries the default route")
    ips = [client] if client else []
    sc = os.environ.get("SSH_CONNECTION", "").split()
    if len(sc) == 4:
        ips.append(sc[0])
    for ip in ips:
        for r in ip_json("route", "get", ip):
            if r.get("dev") in ports:
                risks.append(f"your session ({ip}) uses {r['dev']}")
    return sorted(set(risks))


# ----------------------------------------------------------------------------- netem
def netem_cmd(dev, p, speed_mbit):
    if not any(p[k] > 0 for k in ("delay", "jitter", "loss", "dup", "corrupt", "reorder", "rate")):
        return None
    bw = p["rate"] * 1000 if p["rate"] else max(speed_mbit, 1) * 1_000_000
    lim = max(1000, min(200000, int(bw / 8 * (p["delay"] + 3 * p["jitter"]) / 1e6) + 100))
    a = ["tc", "qdisc", "replace", "dev", dev, "root", "netem", "limit", str(lim)]
    if p["delay"] > 0 or p["jitter"] > 0:
        a += ["delay", f"{(p['delay'] or p['jitter']):g}ms"]
        if p["jitter"] > 0:
            a += [f"{p['jitter']:g}ms", "distribution", "normal"]
    if p["loss"] > 0:
        if p["burst"] > 1 and p["loss"] < 100:
            # Gilbert-Elliott: every packet in the bad state is lost, mean burst length = 1/r
            r = 100.0 / p["burst"]
            pp = min(100.0, r * p["loss"] / (100.0 - p["loss"]))
            a += ["loss", "gemodel", f"{pp:g}%", f"{r:g}%", "100%", "0%"]
        else:
            a += ["loss", "random", f"{p['loss']:g}%"]
    if p["dup"] > 0:
        a += ["duplicate", f"{p['dup']:g}%"]
    if p["corrupt"] > 0:
        a += ["corrupt", f"{p['corrupt']:g}%"]
    if p["reorder"] > 0:
        a += ["reorder", f"{p['reorder']:g}%"]
    if p["rate"] > 0:
        a += ["rate", f"{p['rate']}kbit"]
    return a


def set_iface_mtu(dev, want):
    """Change the MTU of an interface. USB adapters are taken down for the change and checked afterwards
    (a dead adapter is reported; the watchdog then re-initializes it). NICs like macb that refuse a change
    while running are also taken down."""
    if not dev or not want or read_int(f"/sys/class/net/{dev}/mtu", 0) == want:
        return
    usb = bool(usb_info(dev)) if os.path.isdir(f"/sys/class/net/{dev}") else False
    err = ""
    for attempt in (1, 2):
        if os.path.isdir(f"/sys/class/net/{dev}"):
            if usb:
                run(["ip", "link", "set", "dev", dev, "down"], timeout=8)
            rc, _o, err = run(["ip", "link", "set", "dev", dev, "mtu", str(want)], timeout=8)
            if rc and "busy" in (err or "").lower() and not usb:
                run(["ip", "link", "set", "dev", dev, "down"], timeout=8)
                rc, _o, err = run(["ip", "link", "set", "dev", dev, "mtu", str(want)], timeout=8)
                usb = True  # bring it up again below
            if usb:
                run(["ip", "link", "set", "dev", dev, "up"], timeout=8)
            if not rc:
                if usb and nic_dead(dev):
                    raise SimError(f"{dev} stopped responding after the MTU change - "
                                   "automatic USB recovery will follow (the MTU is applied again afterwards)")
                return
        else:
            err = "No such device"
        if attempt == 1:
            time.sleep(2)
    raise SimError(f"MTU {want} on {dev} failed: {err or 'error'}"
                   + (" - interface is gone, check dmesg (USB reset?)" if "No such device" in (err or "") else ""))


def apply_mtu(cfg):
    """Per direction MTU.
    Bridge: both ports get the largest configured MTU (needed for frames above the default, never below the
    original MTU); a direction with a smaller MTU is limited by a tc filter on its egress port
    (A->B on port B, B->A on port A) that drops frames larger than MTU + 14 byte Ethernet header.
    Single: the MTU of the interface is set (OUT value; 0 = original MTU from the snapshot)."""
    snap = load_json(ORIG_FILE) or {}
    if cfg["mode"] == "single":
        set_iface_mtu(cfg["single"]["iface"], cfg["a2b"]["mtu"] or snap.get("mtu"))
        return
    b = cfg["bridge"]
    pa, pb = b["port_a"], b["port_b"]
    if not pa or not pb:
        return
    orig = {p: snap.get("ports", {}).get(p, {}).get("mtu") or read_int(f"/sys/class/net/{p}/mtu", 1500)
            for p in (pa, pb)}
    top = max(cfg["a2b"]["mtu"], cfg["b2a"]["mtu"])
    for p in (pa, pb):
        set_iface_mtu(p, max(orig[p], top))
    for dev, want in ((pb, cfg["a2b"]["mtu"]), (pa, cfg["b2a"]["mtu"])):
        run(["tc", "filter", "del", "dev", dev, "egress", "prio", "1"])
        cur = read_int(f"/sys/class/net/{dev}/mtu", 1500)
        # an explicit MTU always gets an exact limit (the bridge itself would allow MTU + 18 byte frames);
        # a direction left at the default is limited to the original MTU while the ports are raised
        limit = want or (orig[dev] if cur > orig[dev] else 0)
        if limit:
            rc, _o, err = run(["tc", "qdisc", "add", "dev", dev, "clsact"])
            if rc and "clsact" not in run(["tc", "qdisc", "show", "dev", dev, "clsact"])[1]:
                raise SimError(f"MTU filter on {dev}: {(err or 'tc qdisc add clsact failed').strip()}")
            err = ""
            for ident in ("pkt_len", "len", "pktlen"):  # spelling differs between iproute2 versions
                rc, _o, err = run(["tc", "filter", "add", "dev", dev, "egress", "prio", "1", "protocol", "all",
                                   "basic", "match", f"meta({ident} gt {limit + 14})", "action", "drop"])
                if not rc:
                    break
                if "unknown meta id" not in (err or ""):
                    break
            else:
                rc = 1
            if rc:
                raise SimError(f"MTU filter on {dev} failed: {(err or '').strip().splitlines()[0] if err else 'tc error'}"
                               " - list the meta ids with: tc filter add dev " + dev + " basic match 'meta(list)'")
        else:
            run(["tc", "qdisc", "del", "dev", dev, "clsact"])


BLOCK = [False]  # STOP: the setup stays, but every WAN device drops all traffic (netem loss 100 %)


def detect_block(cfg):
    for dev in wan_devs(cfg):
        if dev and "loss 100%" in run(["tc", "qdisc", "show", "dev", dev, "root"])[1]:
            return True
    return False


def set_fwd_mask(name, on):
    """forward the link-local group addresses 01-80-C2-00-00-xx (802.1X EAPOL 03, LLDP 0E, GARP, ...) through the bridge.
    STP BPDUs (00) are forwarded anyway because STP is off; pause (01) and LACP/slow protocols (02) cannot be
    forwarded by a Linux bridge. CDP, VTP, DTP, UDLD, PAgP, PVST+ (01-00-0C-CC-CC-xx) are plain multicast and flood."""
    for mask in (("0xfff8", "0x4008", "0x4000") if on else ("0",)):
        if not run(["ip", "link", "set", "dev", name, "type", "bridge", "group_fwd_mask", mask])[0]:
            return mask
    return None


def apply_bridge_opts(cfg):
    """bridge options that can change while running: LLDP forwarding and the NIC offloads of the ports"""
    if cfg["mode"] != "bridge":
        return
    b = cfg["bridge"]
    if not b["name"] or not os.path.isdir(f"/sys/class/net/{b['name']}"):
        return
    set_fwd_mask(b["name"], b["lldp"])
    snap = load_json(ORIG_FILE) or {}
    for p in (b["port_a"], b["port_b"]):
        if not p or not os.path.isdir(f"/sys/class/net/{p}"):
            continue
        if b.get("offloads_off", True):
            run(["ethtool", "-K", p, "gro", "off", "gso", "off", "tso", "off", "lro", "off"])
        else:  # back to the values the NIC had before the setup
            feats = snap.get("ports", {}).get(p, {}).get("features") or {}
            if feats:
                args = ["ethtool", "-K", p]
                for k, v in feats.items():
                    args += [k, v]
                run(args)


def apply_netem(cfg, strict=True):
    """returns an MTU error text in non-strict mode (used by the USB recovery), else raises"""
    apply_bridge_opts(cfg)
    mtu_err = ""
    try:
        apply_mtu(cfg)
    except SimError as e:
        if strict:
            raise
        mtu_err = str(e)
    for d, dev in zip(("a2b", "b2a"), wan_devs(cfg)):
        if not dev:
            continue
        phys = cfg["single"]["iface"] if cfg["mode"] == "single" else dev
        speed = read_int(f"/sys/class/net/{phys}/speed", 1000)
        cmd = netem_cmd(dev, cfg[d], speed if speed > 0 else 1000)
        if BLOCK[0]:
            cmd = ["tc", "qdisc", "replace", "dev", dev, "root", "netem", "loss", "100%"]
        if cmd is None:
            run(["tc", "qdisc", "del", "dev", dev, "root"])
        else:
            must(cmd)
    return mtu_err


# ----------------------------------------------------------------------------- dependencies
def check_deps():
    miss = [t for t in ("ip", "tc", "ethtool") if not shutil.which(t)]
    if run(["modprobe", "sch_netem"])[0] and not os.path.isdir("/sys/module/sch_netem"):
        miss.append("sch_netem (kernel module)")
    if not os.path.isdir("/sys/module/bridge") and run(["modprobe", "bridge"])[0]:
        miss.append("bridge (kernel module)")
    return miss


def install_deps(log):
    if os.geteuid() != 0:
        raise SimError("root privileges required")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")

    def sh(cmd):
        log("$ " + " ".join(cmd))
        rc, out, err = run(cmd, timeout=900, env=env)
        for ln in (out + "\n" + err).strip().splitlines()[-4:]:
            log("  " + ln)
        return rc

    apt = shutil.which("apt-get")
    pm = shutil.which("dnf") or shutil.which("yum")
    if apt:
        sh([apt, "update"])
        rc = sh([apt, "install", "-y", "iproute2", "ethtool"])
    elif pm:
        rc = sh([pm, "install", "-y", "iproute", "iproute-tc", "ethtool"])
    else:
        raise SimError("No supported package manager (apt-get, dnf, yum)")
    if rc:
        raise SimError("Package installation failed")
    for mod in ("sch_netem", "ifb"):
        if run(["modprobe", mod])[0]:
            if apt:
                sh([apt, "install", "-y", f"linux-modules-extra-{os.uname().release}"])
            elif pm:
                sh([pm, "install", "-y", "kernel-modules-extra"])
            if run(["modprobe", mod])[0]:
                log(f"  warning: kernel module {mod} not available")
    try:
        with open(MODLOAD_FILE, "w") as f:
            f.write("sch_netem\nbridge\nifb\n")
    except OSError:
        pass
    miss = check_deps()
    if miss:
        raise SimError("Still missing: " + ", ".join(miss))
    log("All requirements installed.")


def install_service():
    if os.path.abspath(__file__) != INSTALL_PATH:
        shutil.copy2(os.path.abspath(__file__), INSTALL_PATH)
    os.chmod(INSTALL_PATH, 0o755)
    with open(UNIT_FILE, "w") as f:
        f.write(f"""[Unit]
Description=wansime WAN simulator (web GUI on port {WEB_PORT})
After=network.target

[Service]
Type=simple
User=root
ExecStart={sys.executable} {INSTALL_PATH}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
""")
    must(["systemctl", "daemon-reload"])
    must(["systemctl", "enable", "--now", "wansime.service"])
    return f"Service wansime.service installed and started ({INSTALL_PATH}); web GUI on port {WEB_PORT}"


def remove_service():
    run(["systemctl", "disable", "--now", "wansime.service"])
    for p in (UNIT_FILE, INSTALL_PATH):
        try:
            os.remove(p)
        except OSError:
            pass
    run(["systemctl", "daemon-reload"])
    return "Service wansime.service removed"


# ----------------------------------------------------------------------------- snapshot / bridge / single
FEATS = {"gro": "generic-receive-offload", "gso": "generic-segmentation-offload",
         "tso": "tcp-segmentation-offload", "lro": "large-receive-offload"}
BRNF = ["bridge-nf-call-iptables", "bridge-nf-call-ip6tables", "bridge-nf-call-arptables"]


def get_features(dev):
    out = run(["ethtool", "-k", dev])[1]
    res = {}
    for k, long in FEATS.items():
        m = re.search(rf"^\s*{long}:\s*(on|off)", out, re.M)
        if m:
            res[k] = m.group(1)
    return res


def nm_state(dev):
    if not shutil.which("nmcli"):
        return None
    rc, out, _ = run(["nmcli", "-t", "-f", "GENERAL.STATE", "dev", "show", dev])
    if rc:
        return None
    return "unmanaged" if "unmanaged" in out else "managed"


def take_snapshot(name, ports):
    snap = {"mode": "bridge", "bridge": name, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "ports": {},
            "brnf": {k: read_text(f"/proc/sys/net/bridge/{k}") for k in BRNF
                     if os.path.exists(f"/proc/sys/net/bridge/{k}")}}
    for p in ports:
        link = (ip_json("addr", "show", "dev", p) or [{}])[0]
        snap["ports"][p] = {
            "up": "UP" in link.get("flags", []),
            "mtu": link.get("mtu"),
            "addrs": [f'{a["local"]}/{a["prefixlen"]}' for a in link.get("addr_info", [])
                      if a.get("family") in ("inet", "inet6") and a.get("scope") == "global"],
            "routes": [{"dst": r.get("dst"), "gateway": r.get("gateway"), "metric": r.get("metric")}
                       for r in ip_json("route", "show", "dev", p) if r.get("protocol") != "kernel"],
            "features": get_features(p),
            "nm": nm_state(p),
        }
    return snap


def setup_bridge(cfg, force=False, client=None):
    b = cfg["bridge"]
    name, pa, pb = b["name"], b["port_a"], b["port_b"]
    if not pa or not pb or pa == pb:
        raise SimError("Select two different ports (A and B) first")
    for p in (pa, pb):
        base = f"/sys/class/net/{p}"
        if not os.path.isdir(base):
            raise SimError(f"Interface {p} not found")
        if os.path.exists(base + "/bridge") or os.path.islink(base + "/master"):
            raise SimError(f"{p} is a bridge or already a bridge/bond member")
    if os.path.exists(f"/sys/class/net/{name}"):
        raise SimError(f"{name} already exists (not created by {PROG}) - choose another bridge name")
    risks = mgmt_risk([pa, pb], client)
    if risks and not force:
        raise Warn("Management access may be lost: " + "; ".join(risks))
    save_json(ORIG_FILE, take_snapshot(name, [pa, pb]))
    try:
        for p in (pa, pb):
            if shutil.which("nmcli") and nm_state(p):
                run(["nmcli", "dev", "set", p, "managed", "no"])
            must(["ip", "addr", "flush", "dev", p])
        must(["ip", "link", "add", "name", name, "type", "bridge", "stp_state", "0", "forward_delay", "0"])
        # transparent like a wire: flood multicast to all ports instead of IGMP/MLD snooping
        run(["ip", "link", "set", "dev", name, "type", "bridge", "mcast_snooping", "0"])
        run(["sysctl", "-q", "-w", f"net.ipv6.conf.{name}.disable_ipv6=1"])  # the bridge itself stays silent (no MLD / ND)
        if b["lldp"]:
            set_fwd_mask(name, True)
        for p in (pa, pb):
            must(["ip", "link", "set", "dev", p, "master", name])
            must(["ip", "link", "set", "dev", p, "up"])
            if b.get("offloads_off", True):
                run(["ethtool", "-K", p, "gro", "off", "gso", "off", "tso", "off", "lro", "off"])
        must(["ip", "link", "set", "dev", name, "up"])
        for k in BRNF:
            f = f"/proc/sys/net/bridge/{k}"
            if os.path.exists(f):
                try:
                    with open(f, "w") as fh:
                        fh.write("0")
                except OSError:
                    pass
    except Exception:
        try:
            restore_all()
        except Exception:
            pass
        raise


def restore_bridge(snap):
    name = snap.get("bridge", "")
    ports = list(snap.get("ports", {}))
    problems = []

    def q(cmd):
        return run(cmd, timeout=8)

    def step(cmd):
        rc, _o, err = q(cmd)
        if rc:
            problems.append(err or " ".join(cmd))
        return rc

    for p in ports:
        q(["tc", "qdisc", "del", "dev", p, "root"])
        q(["tc", "qdisc", "del", "dev", p, "clsact"])
    if bridge_active(name):
        for p in os.listdir(f"/sys/class/net/{name}/brif"):
            step(["ip", "link", "set", "dev", p, "nomaster"])
        q(["ip", "link", "set", "dev", name, "down"])
        step(["ip", "link", "del", name])
    for k, v in snap.get("brnf", {}).items():
        try:
            with open(f"/proc/sys/net/bridge/{k}", "w") as fh:
                fh.write(v)
        except OSError:
            pass
    managed = []
    for p, sp in snap.get("ports", {}).items():
        if not os.path.isdir(f"/sys/class/net/{p}"):
            problems.append(f"{p}: interface not present (USB adapter reset?)")
            continue
        if sp.get("features"):
            args = ["ethtool", "-K", p]
            for k, v in sp["features"].items():
                args += [k, v]
            q(args)
        if sp.get("mtu"):
            q(["ip", "link", "set", "dev", p, "mtu", str(sp["mtu"])])
        if sp.get("nm") == "managed":
            q(["nmcli", "dev", "set", p, "managed", "yes"])
            managed.append(p)
        else:
            for a in sp.get("addrs", []):
                q(["ip", "addr", "add", a, "dev", p])
        q(["ip", "link", "set", "dev", p, "up" if sp.get("up") else "down"])
        if sp.get("nm") != "managed":
            for r in sp.get("routes", []):
                cmd = ["ip", "route", "add", r["dst"]]
                if r.get("gateway"):
                    cmd += ["via", r["gateway"]]
                cmd += ["dev", p]
                if r.get("metric"):
                    cmd += ["metric", str(r["metric"])]
                q(cmd)
    for p in managed:
        q(["nmcli", "dev", "connect", p])
    if bridge_active(name):
        raise SimError(f"Bridge {name} could not be removed ({'; '.join(problems) or 'unknown'}) - "
                       "snapshot kept: run restore again, replug the USB adapter or reboot")
    try:
        os.remove(ORIG_FILE)
    except OSError:
        pass
    msg = f"Original state restored (bridge {name} removed, ports {', '.join(ports)} reset)"
    if problems:
        msg += " - warnings: " + "; ".join(problems)
    return msg


def single_attach(dev):
    must(["tc", "qdisc", "add", "dev", dev, "handle", "ffff:", "ingress"])
    must(["tc", "filter", "add", "dev", dev, "parent", "ffff:", "protocol", "all", "u32", "match", "u32",
          "0", "0", "action", "mirred", "egress", "redirect", "dev", IFB])


def setup_single(cfg, force=False, client=None):
    dev = cfg["single"]["iface"]
    if not dev:
        raise SimError("Select an interface first")
    if not os.path.isdir(f"/sys/class/net/{dev}"):
        raise SimError(f"Interface {dev} not found")
    if os.path.exists(f"/sys/class/net/{IFB}"):
        raise SimError(f"{IFB} already exists")
    if "ingress" in run(["tc", "qdisc", "show", "dev", dev, "ingress"])[1]:
        raise SimError(f"{dev} already has an ingress qdisc (not created by {PROG})")
    risks = mgmt_risk([dev], client)
    if risks and not force:
        raise Warn(f"Management traffic on {dev} will be impaired as well: " + "; ".join(risks))
    save_json(ORIG_FILE, {"mode": "single", "iface": dev, "ifb": IFB,
                         "mtu": read_int(f"/sys/class/net/{dev}/mtu", 0), "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        if run(["modprobe", "ifb", "numifbs=0"])[0] and not os.path.isdir("/sys/module/ifb"):
            raise SimError(f"Kernel module ifb not available - run: {PROG} --install")
        must(["ip", "link", "add", IFB, "type", "ifb"])
        must(["ip", "link", "set", IFB, "up"])
        single_attach(dev)
    except Exception:
        try:
            restore_all()
        except Exception:
            pass
        raise


def restore_single(snap):
    dev, ifb = snap.get("iface", ""), snap.get("ifb", IFB)
    run(["tc", "qdisc", "del", "dev", dev, "root"])
    run(["tc", "qdisc", "del", "dev", dev, "ingress"])
    run(["tc", "qdisc", "del", "dev", ifb, "root"])
    run(["ip", "link", "del", ifb])
    if snap.get("mtu"):
        run(["ip", "link", "set", "dev", dev, "mtu", str(snap["mtu"])])
    try:
        os.remove(ORIG_FILE)
    except OSError:
        pass
    return f"Original state restored (impairments and {ifb} removed from {dev})"


def restore_all():
    snap = load_json(ORIG_FILE)
    if not snap:
        raise SimError("Nothing to restore (no snapshot found)")
    BLOCK[0] = False
    return restore_single(snap) if snap.get("mode") == "single" else restore_bridge(snap)


def same_target(snap, c):
    if c["mode"] == "single":
        return snap.get("mode") == "single" and snap.get("iface") == c["single"]["iface"]
    b = c["bridge"]
    return snap.get("mode", "bridge") == "bridge" and snap.get("bridge") == b["name"] \
        and set(snap.get("ports", {})) == {b["port_a"], b["port_b"]}


# ----------------------------------------------------------------------------- core object
# ----------------------------------------------------------------------------- USB adapter recovery
def usb_info(iface):
    """(usb device e.g. '2-1', driver module e.g. 'r8152') of a USB network interface, else None"""
    base = f"/sys/class/net/{iface}/device"
    try:
        real = os.path.realpath(base)
    except OSError:
        return None
    dev = None
    for part in reversed(real.split("/")):
        m = re.fullmatch(r"(\d+-[\d.]+):\d+\.\d+", part)
        if m:
            dev = m.group(1)
            break
    if not dev:
        return None
    mod = ""
    try:
        mod = os.path.basename(os.readlink(base + "/driver/module"))
    except OSError:
        pass
    return dev, mod


def nic_dead(iface):
    """True if the interface vanished or its driver no longer reaches the (USB) device"""
    if not os.path.isdir(f"/sys/class/net/{iface}"):
        return True
    rc, out, err = run(["ethtool", iface], timeout=4)
    return rc != 0 and "no such device" in (out + err).lower()


def wait_nic(iface, secs):
    end = time.time() + secs
    while time.time() < end:
        if os.path.isdir(f"/sys/class/net/{iface}") and not nic_dead(iface):
            return True
        time.sleep(0.5)
    return False


def recover_nic(iface, dev, mod):
    """bring a hung USB network adapter back: 1) re-authorize that USB device (like unplug/replug),
    2) reload the driver module. Returns a short description of what worked."""
    auth = f"/sys/bus/usb/devices/{dev}/authorized"
    if os.path.exists(auth):
        for v in ("0", "1"):
            try:
                with open(auth, "w") as fh:
                    fh.write(v)
            except OSError:
                break
            time.sleep(1.5)
        if wait_nic(iface, 12):
            return f"USB device {dev} re-authorized"
    if mod:
        run(["modprobe", "-r", mod], timeout=15)
        time.sleep(2)
        run(["modprobe", mod], timeout=15)
        if wait_nic(iface, 12):
            return f"driver {mod} reloaded"
    return ""


def repair_ports(cfg):
    """after a NIC came back: put it into the bridge / re-attach the IFB redirect again"""
    if cfg["mode"] == "single":
        dev = cfg["single"]["iface"]
        if "ingress" not in run(["tc", "qdisc", "show", "dev", dev, "ingress"])[1]:
            single_attach(dev)
        return
    b = cfg["bridge"]
    for p in (b["port_a"], b["port_b"]):
        if not os.path.isdir(f"/sys/class/net/{p}"):
            raise SimError(f"{p} did not come back")
        try:
            member = os.path.basename(os.readlink(f"/sys/class/net/{p}/master")) == b["name"]
        except OSError:
            member = False
        if member:
            continue
        if shutil.which("nmcli") and nm_state(p):
            run(["nmcli", "dev", "set", p, "managed", "no"])
        must(["ip", "addr", "flush", "dev", p])
        must(["ip", "link", "set", "dev", p, "master", b["name"]])
        must(["ip", "link", "set", "dev", p, "up"])
        if b.get("offloads_off", True):
            run(["ethtool", "-K", p, "gro", "off", "gso", "off", "tso", "off", "lro", "off"])


class Sim:
    def __init__(self, show_all=False):
        self.lock = threading.RLock()  # held while a system operation runs
        self.cfg_lock = threading.Lock()  # short: protects self.cfg only
        self.busy = ""
        self.show_all = show_all
        self.link = False
        raw = load_json(CFG_FILE, {})
        self.cfg = copy.deepcopy(DEF_CFG)
        try:
            self.cfg = merge_cfg(self.cfg, raw)
        except SimError:
            pass
        self.saved = copy.deepcopy(self.cfg) if os.path.exists(CFG_FILE) else None
        snap = load_json(ORIG_FILE)
        if snap and not snap_alive(snap):
            try:
                os.remove(ORIG_FILE)
            except OSError:
                pass
        self.missing = check_deps()
        BLOCK[0] = bool(snap_alive(load_json(ORIG_FILE))) and detect_block(self.cfg)
        self._cache = {"ifaces": [], "a2b": None, "b2a": None}
        self.usb = {}  # port -> (usb device, driver module), remembered while the NIC is present
        self.dead = {}  # port -> consecutive failed health checks
        self.tries = []  # timestamps of automatic recoveries
        self.notice = ""
        self.hist = collections.deque(maxlen=1800)  # [ts, A->B in, A->B out, B->A in, B->A out] bit/s, 1 sample/s
        self.cpu = {}
        self._cpu_old = None
        self.totals = [0.0] * 4                      # bytes since start: A->B in/out, B->A in/out
        self._last = None
        self._skey = None
        self._refresh_once()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    # slow system queries (ip, tc) run in the background; state() only reads the cache
    def _refresh_once(self):
        try:
            da, db = wan_devs(self.get_cfg())
            self._cache = {
                "ifaces": iface_info(self.show_all),
                "a2b": netem_stats(da) if da and os.path.isdir(f"/sys/class/net/{da}") else None,
                "b2a": netem_stats(db) if db and os.path.isdir(f"/sys/class/net/{db}") else None,
            }
        except Exception:
            pass

    def ports(self, c=None):
        c = c or self.get_cfg()
        ports = [c["single"]["iface"]] if c["mode"] == "single" else [c["bridge"]["port_a"], c["bridge"]["port_b"]]
        return [p for p in ports if p]

    def _watchdog(self):
        """a hung USB adapter (netdev still there, device gone for the driver) is recovered automatically"""
        if self.busy or not self.active():
            return
        for p in self.ports():
            if os.path.isdir(f"/sys/class/net/{p}"):
                info = usb_info(p)
                if info:
                    self.usb[p] = info
            if p not in self.usb:
                continue  # not a USB adapter
            self.dead[p] = self.dead.get(p, 0) + 1 if nic_dead(p) else 0
            if self.dead[p] >= 2:
                now = time.time()
                self.tries = [t for t in self.tries if now - t < 600]
                if len(self.tries) >= 3:
                    self.notice = f"{p} keeps failing - automatic recovery stopped (use the Recover button)"
                    return
                self.tries.append(now)
                self.dead[p] = 0
                threading.Thread(target=self._auto_recover, daemon=True).start()
                return

    def _auto_recover(self):
        try:
            self.notice = self.recover(auto=True)
        except (SimError, Warn) as e:
            self.notice = f"Recovery: {e}"

    def _sample(self):
        """traffic history for the graphs: rates of the last second per direction (IN = offered, OUT = passed)"""
        c = self.get_cfg()
        single = c["mode"] == "single"
        pa, pb = (c["single"]["iface"], "") if single else (c["bridge"]["port_a"], c["bridge"]["port_b"])
        key = (c["mode"], pa, pb)
        if key != self._skey:
            self._skey, self._last, self.totals = key, None, [0.0] * 4
        now = time.time()
        a = counters(pa) if pa and os.path.isdir(f"/sys/class/net/{pa}") else None
        b = counters(pb) if pb and os.path.isdir(f"/sys/class/net/{pb}") else None
        last, self._last = self._last, (now, a, b)
        vals = [0.0] * 4
        if last and last[1] and a and (single or (last[2] and b)):
            dt = now - last[0]

            def r(cur, old, k):
                d = cur[k] - old[k]
                return d * 8 / dt if d >= 0 and dt > 0 else 0.0
            if single:
                o = r(a, last[1], "tx_bytes")
                i = r(a, last[1], "rx_bytes")
                vals = [o, o, i, i]
            else:
                vals = [r(a, last[1], "rx_bytes"), r(b, last[2], "tx_bytes"),
                        r(b, last[2], "rx_bytes"), r(a, last[1], "tx_bytes")]
            for k in range(4):
                self.totals[k] += vals[k] * dt / 8
        self.hist.append([round(now, 1)] + [round(v) for v in vals])
        self._cpu_sample(now)

    def _cpu_sample(self, now):
        # CPU load: whole system, busiest core and this process (percent of one core)
        try:
            cores = {}
            with open("/proc/stat") as f:
                for ln in f:
                    if ln.startswith("cpu"):
                        p = ln.split()
                        v = [int(x) for x in p[1:9]]
                        cores[p[0]] = (sum(v), v[3] + v[4])  # total, idle+iowait
            t = os.times()
            cur = (now, cores, t.user + t.system)
            old, self._cpu_old = self._cpu_old, cur
            if not old or now <= old[0]:
                return
            pct = {}
            for k, (tot, idle) in cores.items():
                o = old[1].get(k)
                if o and tot > o[0]:
                    pct[k] = max(0.0, min(100.0, 100.0 * (1 - (idle - o[1]) / (tot - o[0]))))
            allc = pct.pop("cpu", 0.0)
            self.cpu = {"tot": round(allc), "max": round(max(pct.values())) if pct else round(allc),
                        "proc": round(100.0 * (cur[2] - old[2]) / (now - old[0])), "n": len(pct)}
        except (OSError, ValueError):
            pass

    def reset_hist(self):
        self.hist.clear()
        self.totals = [0.0] * 4
        self._last = None
        return "Traffic graphs reset"

    def history(self, since=0, limit=600):
        for _ in range(3):
            try:
                h = list(self.hist)
                break
            except RuntimeError:
                h = []
        return [x for x in h if x[0] > since][-limit:]

    def _refresh_loop(self):
        n = 0
        while True:
            time.sleep(1.0)
            self._refresh_once()
            try:
                self._sample()
            except Exception:
                pass
            n += 1
            if n % 3 == 0:
                try:
                    self._watchdog()
                except Exception:
                    pass

    def get_cfg(self):
        with self.cfg_lock:
            return copy.deepcopy(self.cfg)

    @contextlib.contextmanager
    def op(self, what):
        if not self.lock.acquire(blocking=False):
            raise SimError(f"Busy: {self.busy or 'another operation'} is still running")
        self.busy = what
        try:
            yield
        finally:
            self.busy = ""
            self.lock.release()

    def active(self):
        return snap_alive(load_json(ORIG_FILE))

    def set_cfg(self, new):
        with self.cfg_lock:
            self.cfg = merge_cfg(self.cfg, new)

    def apply_now(self):
        """apply the WAN parameters of the active setup (used after a short delay once changes stop)"""
        if not self.active():
            return "Value set (not applied - no active setup)"
        if BLOCK[0]:
            return "Value set (stopped - applied on START)"
        with self.op("applying WAN parameters"):
            apply_netem(self.get_cfg())
        return "WAN parameters applied"

    def set_value(self, d, key, val, apply=True):
        c = self.get_cfg()
        dirs = [d] + ([("b2a" if d == "a2b" else "a2b")] if self.link else [])
        self.set_cfg({x: {**c[x], key: val} for x in dirs})
        n = self.get_cfg()
        raised = any(n[x]["delay"] > (val if key == "delay" else c[x]["delay"]) for x in dirs)
        note = ""
        if raised:
            nd = max(n[x]["delay"] for x in dirs)
            why = "reorder needs a delay" if any(n[x]["reorder"] > 0 and n[x]["delay"] == REORDER_MIN_DELAY for x in dirs) \
                else "jitter needs a delay"
            note = f" - delay set to {nd:g} ms ({why})"
        if not apply:
            return ("Value set - applying in a moment" if self.active() else "Value set (no active setup)") + note
        return self.apply_now() + note

    def stop(self):
        """STOP: setup stays in place, no traffic passes until START"""
        if not self.active():
            raise SimError("Not running - press START first")
        with self.op("stopping traffic"):
            BLOCK[0] = True
            apply_netem(self.get_cfg())
        return "STOPPED - no traffic passes (START resumes)"

    def apply(self, force=False, client=None):
        with self.op("setting up and applying"):
            c = self.get_cfg()
            BLOCK[0] = False
            snap = load_json(ORIG_FILE)
            if snap_alive(snap):
                if not same_target(snap, c):
                    restore_all()
                    snap = None
            elif snap:
                os.remove(ORIG_FILE)
                snap = None
            if not snap:
                (setup_single if c["mode"] == "single" else setup_bridge)(c, force, client)
            apply_netem(c)
            if c["mode"] == "single":
                return f"Single interface {c['single']['iface']} active, WAN parameters applied"
            return f"Bridge {c['bridge']['name']} active, WAN parameters applied"

    def clear(self):
        self.set_cfg({"a2b": dict(DEF_DIR), "b2a": dict(DEF_DIR)})
        if self.active():
            with self.op("clearing impairments"):
                apply_netem(self.get_cfg())
        return "All impairments cleared"

    def preset(self, name):
        if name not in PRESETS:
            raise SimError("Unknown preset")
        c = self.get_cfg()  # presets keep the MTU setting
        self.set_cfg({"a2b": {**PRESETS[name][0], "mtu": c["a2b"]["mtu"]},
                      "b2a": {**PRESETS[name][1], "mtu": c["b2a"]["mtu"]}})
        if self.active():
            with self.op("applying preset"):
                apply_netem(self.get_cfg())
            return f"Preset '{name}' loaded and applied"
        return f"Preset '{name}' loaded"

    def recover(self, auto=False):
        """recover hung USB adapters of the active setup, rebuild bridge membership and WAN parameters"""
        with self.op("recovering network adapter"):
            c = self.get_cfg()
            if not self.active():
                raise SimError("No active setup")
            done = []
            for p in self.ports(c):
                info = usb_info(p) if os.path.isdir(f"/sys/class/net/{p}") else None
                info = info or self.usb.get(p)
                if not info:
                    continue  # not a USB adapter
                if auto and not nic_dead(p):
                    continue
                how = recover_nic(p, info[0], info[1])
                if not how:
                    raise SimError(f"{p}: recovery failed - replug the USB adapter or reboot")
                done.append(f"{p}: {how}")
            if not done:
                return "No USB adapter problem found"
            repair_ports(c)
            mtu_err = apply_netem(c, strict=False)
            return ("Recovered - " + "; ".join(done) + " - bridge and WAN parameters restored"
                    + (f" (MTU not applied: {mtu_err})" if mtu_err else ""))

    def restore(self):
        with self.op("restoring original state"):
            return restore_all()

    def save(self):
        c = self.get_cfg()
        save_json(CFG_FILE, c)
        self.saved = copy.deepcopy(c)
        return f"Config saved to {CFG_FILE}"

    def reload_deps(self):
        self.missing = check_deps()

    def state(self):
        c = self.get_cfg()
        if c["mode"] == "single":
            pa, pb = c["single"]["iface"], ""
        else:
            pa, pb = c["bridge"]["port_a"], c["bridge"]["port_b"]
        cache = self._cache
        return {
            "version": VERSION, "credit": CREDIT, "mode": c["mode"], "cfg": c,
            "params": [[k, l, u, *SLIDER[k]] for k, l, u, *_ in PARAMS], "reorder_min_delay": REORDER_MIN_DELAY,
            "presets": list(PRESETS), "ifaces": cache["ifaces"], "notice": self.notice, "cfg_file": CFG_FILE,
            "default_mtu": default_mtu(c, cache["ifaces"]),
            "active": self.active(), "stopped": BLOCK[0] and self.active(), "totals": [int(x) for x in self.totals], "cpu": self.cpu, "busy": self.busy, "missing": self.missing,
            "saved": self.saved == c, "has_file": self.saved is not None,
            "labels": dir_labels(c), "ts": time.time(),
            "stats": {
                "a": counters(pa) if pa and os.path.isdir(f"/sys/class/net/{pa}") else None,
                "b": counters(pb) if pb and os.path.isdir(f"/sys/class/net/{pb}") else None,
                "a2b": cache["a2b"],
                "b2a": cache["b2a"],
            },
        }



# ----------------------------------------------------------------------------- web GUI
HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>wansime.py</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0iIzRhM2IzNCI+CiAgPGVsbGlwc2UgY3g9IjEyIiBjeT0iMTYuNSIgcng9IjUuNCIgcnk9IjQuMyIvPgogIDxlbGxpcHNlIGN4PSI1LjIiIGN5PSIxMC41IiByeD0iMi41IiByeT0iMy4xIi8+CiAgPGVsbGlwc2UgY3g9IjE4LjgiIGN5PSIxMC41IiByeD0iMi41IiByeT0iMy4xIi8+CiAgPGVsbGlwc2UgY3g9IjguOSIgY3k9IjUuNiIgcng9IjIuNCIgcnk9IjMuMSIvPgogIDxlbGxpcHNlIGN4PSIxNS4xIiBjeT0iNS42IiByeD0iMi40IiByeT0iMy4xIi8+Cjwvc3ZnPg==">
<style>
:root{--bg:#0b0f0b;--fg:#c8d6c8;--dim:#b3c2b3;--line:#1e2a1e;--up:#3ddc60;--down:#ff4b4b;--acc:#7fd1ff;--panel:#101610;--ctrl-line:#42593f}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace;font-size:13px}
header{padding:6px 12px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;justify-content:space-between}
.top{display:flex;align-items:flex-start}.topl{flex:1 1 auto;min-width:0}.hr{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 12px 0 8px}.nelly{display:block;line-height:0}.nelly svg{height:60px;width:auto;display:block}
.title{font-weight:700;letter-spacing:.5px}.title small{color:var(--dim);font-weight:400}
.title a{color:inherit;text-decoration:underline;text-decoration-color:var(--line)}.title a:hover{color:var(--acc);text-decoration-color:currentColor}
.clock{color:var(--dim)}
.bar{display:flex;flex-wrap:wrap;gap:6px;padding:6px 12px;border-bottom:1px solid var(--line);align-items:center}
button{background:var(--panel);color:var(--fg);border:1px solid var(--ctrl-line);padding:4px 10px;cursor:pointer;font:inherit;border-radius:3px}
button:hover{border-color:var(--acc);color:var(--acc)}button.on{border-color:var(--up);color:var(--up)}
button.danger:hover{border-color:var(--down);color:var(--down)}
input[type=text],input[type=number]{background:var(--panel);color:var(--fg);border:1px solid var(--ctrl-line);padding:4px 8px;font:inherit;border-radius:3px}
input[type=text]{min-width:120px;width:140px}input[type=number]{width:92px;min-width:0}
input:focus{outline:none;border-color:var(--acc)}input:disabled,input:disabled+*{opacity:.4}
select{background:var(--panel) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23b3c2b3' stroke-width='1.3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat right 9px center;background-size:9px 5px;color:var(--fg);border:1px solid var(--ctrl-line);padding:4px 22px 4px 8px;font:inherit;border-radius:3px;-webkit-appearance:none;-moz-appearance:none;appearance:none}
select.sm{font-size:12px;padding:3px 20px 3px 6px}select:hover{border-color:var(--acc)}select.on{border-color:var(--up);color:var(--up)}
input[type=range]{accent-color:var(--acc)}input[type=checkbox]{accent-color:var(--acc)}
.chk{display:flex;align-items:center;gap:6px;color:var(--fg);font-size:12px;cursor:pointer}
.sep{color:var(--dim);user-select:none}
.stats{display:flex;flex-wrap:wrap;gap:16px;padding:5px 12px;border-bottom:1px solid var(--line);color:var(--dim)}
.stats b{color:var(--fg);font-weight:600}.stats .u b{color:var(--up)}.stats .d b{color:var(--down)}
.msg{color:var(--acc)}.msg.pending{color:var(--up)}.msg.err{color:var(--down)}
.banner[hidden]{display:none}.banner{padding:6px 12px;background:#2a1414;color:var(--down);border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.cpu{margin-left:14px;font-weight:400;letter-spacing:0}.cpu b{color:var(--fg)}.cpu b.hi{color:var(--down,#ff5c5c)}
.sech{padding:8px 12px 2px;color:var(--dim);letter-spacing:.5px;font-weight:600}
.wrap{padding:2px 12px 6px;overflow-x:auto}
table{border-collapse:collapse;min-width:60%}
#wan{width:100%}#wan td:nth-child(2),#wan td:nth-child(3){width:38%}
th,td{padding:2px .8ch;white-space:nowrap;text-align:left;border-bottom:1px solid #131b13}
th{color:var(--dim);font-weight:600;border-bottom:1px solid var(--line)}
td.state{font-weight:700}td.up,.up{color:var(--up)}td.down,.down{color:var(--down)}.mut{color:var(--dim)}.bold{font-weight:700}
.sl{display:flex;gap:8px;align-items:center}.sl input[type=range]{flex:1 1 auto;min-width:80px;width:100%;padding:0}.sl input[type=number]{flex:0 0 92px;-moz-appearance:textfield;appearance:textfield}input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.pm{display:flex;gap:4px;flex:0 0 auto}button.st{padding:2px 9px;min-width:28px;line-height:1.2}
.hide{display:none!important}
.graphs{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:4px 16px;padding:2px 12px 8px}
.gt{padding:2px 0;color:var(--dim)}.gt b{color:var(--fg)}.gt .o{color:var(--up)}.gt .i{color:var(--acc)}
canvas{width:100%;height:150px;display:block;background:var(--panel);border:1px solid var(--line)}
footer{border-top:1px solid var(--line);padding:4px 12px;color:var(--dim);font-size:12px}
.layout{display:flex;flex-direction:column;min-height:100%}.body{flex:1 1 auto}
</style></head><body><div class="layout">
<div id="deps" class="banner" hidden>MISSING REQUIREMENTS: <b id="miss"></b> - RUN: sudo ./wansime.py --install</div>
<div class="top"><div class="topl">
<header><div class="title">wansime.py <small>@VERSION@</small> &bull; &copy; Ewald Jeitler &bull;
 supervised by <a href="https://jeitler.cc/nelly/" target="_blank" rel="noopener">Nelly</a> &bull;
 <a href="https://tools.jeitler.cc" target="_blank">tools.jeitler.cc</a> &bull; <a href="https://www.jeitler.cc" target="_blank">www.jeitler.cc</a></div></header>
<div class="bar">
 <select id="mode"><option value="bridge">BRIDGE - TRANSPARENT, 2 PORTS</option><option value="single">SINGLE INTERFACE - LOCAL TRAFFIC</option></select>
 <select id="pa" class="m-bridge" data-ph="PORT A"></select><select id="pb" class="m-bridge" data-ph="PORT B"></select>
 <input type="text" id="bn" class="m-bridge" placeholder="bridge name" title="bridge name">
 <select id="si" class="m-single" data-ph="INTERFACE"></select>
 <span class="sep">&nbsp;|&nbsp;</span>
 <label class="chk m-bridge"><input type="checkbox" id="lldp" title="also 802.1X EAPOL and all other link-local 01-80-C2-00-00-xx frames except pause and LACP">FORWARD LLDP / 802.1X</label>
 <label class="chk m-bridge"><input type="checkbox" id="offl">DISABLE NIC OFFLOADS</label>
</div>
<div class="bar">
 <button onclick="rescan()">RESCAN INTERFACES</button>
 <button id="ss" onclick="startStop()">START</button>
 <button class="danger" onclick="if(confirm('Restore original state? Removes the bridge / IFB and resets the ports.'))act('/api/restore',{},1)">RESTORE ORIGINAL STATE</button>
 <button onclick="act('/api/save')">SAVE CONFIG</button>
 <button onclick="if(confirm('Re-initialize the USB adapter(s) of the active setup? Traffic stops for a few seconds.'))act('/api/recover',{},1)">RECOVER USB ADAPTER</button>
 <span class="sep">&nbsp;|&nbsp;</span>
 <select id="pre" class="sm" onchange="if(this.value)act('/api/preset',{name:this.value},1)"></select>
 <label class="chk" title="mirror both directions: a change is applied to both directions"><input type="checkbox" id="link">MIRROR</label>
  <button onclick="act('/api/clear',{},1)">RESET</button>
</div>
</div> <div class="hr"><div class="clock" id="clock"></div>
<a class="nelly" href="https://jeitler.cc/nelly/" target="_blank" rel="noopener" title="supervised by Nelly"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 340" role="img" aria-label="Nelly, a cute cartoon cat">
  <title>Nelly</title>
  <style>
    .lid  { transform: scaleY(0); transform-origin: center; animation: blink 5s infinite; }
    .tail { transform-origin: 152px 300px; animation: wag 2.6s ease-in-out infinite; }
    @keyframes wag { 0%,100% { transform: rotate(0deg) } 50% { transform: rotate(-11deg) } }
    @keyframes blink { 0%,94%,100% { transform: scaleY(0) } 96%,98% { transform: scaleY(1) } }
    @media (prefers-reduced-motion: reduce) { .tail, .lid { animation: none } }
  </style>
  <g class="tail">
    <path d="M152 300 C 220 312, 268 286, 262 236 C 259 208, 232 200, 222 220 C 214 236, 232 244, 240 232"
          pathLength="100" fill="none" stroke="#fdfaf6" stroke-width="17" stroke-linecap="round"/>
    <path d="M152 300 C 220 312, 268 286, 262 236 C 259 208, 232 200, 222 220 C 214 236, 232 244, 240 232"
          pathLength="100" fill="none" stroke="#3f3733" stroke-width="17" stroke-linecap="round"
          stroke-dasharray="24 100" stroke-dashoffset="-6"/>
    <path d="M152 300 C 220 312, 268 286, 262 236 C 259 208, 232 200, 222 220 C 214 236, 232 244, 240 232"
          pathLength="100" fill="none" stroke="#f0a049" stroke-width="17" stroke-linecap="round"
          stroke-dasharray="26 100" stroke-dashoffset="-40"/>
    <path d="M152 300 C 220 312, 268 286, 262 236 C 259 208, 232 200, 222 220 C 214 236, 232 244, 240 232"
          pathLength="100" fill="none" stroke="#3f3733" stroke-width="17" stroke-linecap="round"
          stroke-dasharray="20 100" stroke-dashoffset="-76"/>
  </g>
  <ellipse cx="160" cy="252" rx="86" ry="66" fill="#fdfaf6"/>
  <path d="M92 216 C 108 200, 138 198, 150 214 C 160 228, 140 246, 118 244 C 100 242, 86 230, 92 216 Z" fill="#f0a049"/>
  <path d="M206 288 C 226 280, 240 262, 238 244 C 226 268, 208 276, 192 278 Z" fill="#3f3733"/>
  <ellipse cx="126" cy="308" rx="26" ry="15" fill="#fdfaf6" stroke="#e8ded4" stroke-width="2"/>
  <ellipse cx="194" cy="308" rx="26" ry="15" fill="#fdfaf6" stroke="#e8ded4" stroke-width="2"/>
  <path d="M118 306v7M126 304v9M134 306v7" stroke="#e0d3c7" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M186 306v7M194 304v9M202 306v7" stroke="#e0d3c7" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M84 108 L 82 46 L 132 82 Z" fill="#f0a049"/>
  <path d="M92 100 L 91 64 L 122 86 Z" fill="#ffc0cb"/>
  <path d="M236 108 L 238 46 L 188 82 Z" fill="#3f3733"/>
  <path d="M228 100 L 229 64 L 198 86 Z" fill="#ffc0cb"/>
  <ellipse cx="160" cy="146" rx="88" ry="78" fill="#fdfaf6"/>
  <path d="M160 68 C 200 68, 234 92, 242 128 C 226 140, 200 132, 186 112 C 176 96, 168 78, 160 68 Z" fill="#3f3733"/>
  <path d="M160 68 C 122 70, 92 92, 82 122 C 100 130, 122 120, 134 102 C 143 88, 152 76, 160 68 Z" fill="#f0a049"/>
  <path d="M232 176 C 224 196, 208 210, 190 216 C 200 196, 214 182, 232 176 Z" fill="#f0a049"/>
  <ellipse cx="128" cy="150" rx="15" ry="17" fill="#3f3733"/>
  <ellipse cx="192" cy="150" rx="15" ry="17" fill="#3f3733"/>
  <circle cx="133" cy="144" r="5.5" fill="#fff"/>
  <circle cx="197" cy="144" r="5.5" fill="#fff"/>
  <circle cx="124" cy="157" r="2.5" fill="#fff" opacity=".8"/>
  <circle cx="188" cy="157" r="2.5" fill="#fff" opacity=".8"/>
  <ellipse class="lid" cx="128" cy="150" rx="16" ry="18" fill="#fdfaf6"/>
  <ellipse class="lid" cx="192" cy="150" rx="16" ry="18" fill="#fdfaf6"/>
  <ellipse cx="104" cy="176" rx="15" ry="10" fill="#ffb7c5" opacity=".65"/>
  <ellipse cx="216" cy="176" rx="15" ry="10" fill="#ffb7c5" opacity=".65"/>
  <path d="M152 176 L 168 176 L 160 186 Z" fill="#ff9aa8"/>
  <path d="M160 186 C 160 196, 150 198, 145 192 M160 186 C 160 196, 170 198, 175 192"
        fill="none" stroke="#3f3733" stroke-width="3" stroke-linecap="round"/>
  <circle cx="137" cy="181" r="3.4" fill="#3f3733"/>
  <path d="M96 166 L 60 158 M96 176 L 58 178 M96 186 L 62 196" stroke="#c9b8ab" stroke-width="3" stroke-linecap="round"/>
  <path d="M224 166 L 260 158 M224 176 L 262 178 M224 186 L 258 196" stroke="#c9b8ab" stroke-width="3" stroke-linecap="round"/>
</svg></a></div></div>
<div class="stats">
 <span>MODE: <b id="sMode">-</b></span>
 <span id="sStateW">STATE: <b id="sState">-</b></span>
 <span>CONFIG: <b id="sCfg">-</b></span>
 <span class="msg" id="msg"></span>
</div>
<div class="body">
 <div class="sech">WAN PARAMETERS</div><div class="wrap"><table id="wan"></table></div>
 <div class="sech">LIVE</div><div class="wrap"><table id="live"></table></div>
 <div class="sech">TRAFFIC <select id="rng"><option value="60">LAST 1 MIN</option><option value="300" selected>LAST 5 MIN</option><option value="900">LAST 15 MIN</option><option value="1800">LAST 30 MIN</option></select> <button class="sm" onclick="resetG()">RESET</button> <span id="cpu" class="cpu"></span></div>
 <div class="graphs"><div><div class="gt" id="t0"></div><canvas id="g0"></canvas></div><div><div class="gt" id="t1"></div><canvas id="g1"></canvas></div></div>
 <div class="sech">INTERFACES</div><div class="wrap"><table id="ifs"></table></div>
</div>
<footer>@CREDIT@</footer></div>
<script>
const $=id=>document.getElementById(id);let ST=null,PREV=null,noteT=null,BUSY=0;
async function api(p,b){const o=b===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)};
 try{const r=await fetch(p,o);let j={};try{j=await r.json()}catch(e){}return[r.status,j]}catch(e){return[0,{error:'connection lost'}]}}
function note(t,e,pend){const m=$('msg');m.textContent=t||'';m.className='msg'+(pend?' pending':'')+(e?' err':'');clearTimeout(noteT);
 if(!pend&&t)noteT=setTimeout(()=>{m.textContent='';m.className='msg'},e?8000:2500)}
function fb(b){for(const[l,u]of[[1e9,'Gbit/s'],[1e6,'Mbit/s'],[1e3,'kbit/s']])if(b>=l)return(b/l).toFixed(2)+' '+u;return b.toFixed(0)+' bit/s'}
function sync(d,k,v){const n=document.querySelector(`[data-d="${d}"][data-k="${k}"]`),r=document.querySelector(`[data-rd="${d}"][data-rk="${k}"]`);if(document.activeElement!==n)n.value=v;r.value=v}
let APT=null;
function liveApply(){clearTimeout(APT);APT=setTimeout(liveApplyNow,600)}
async function liveApplyNow(){if(!ST||!ST.active||ST.stopped)return;const[s]=await api('/api/cfg',form());if(s!==200)return;
 note('applying ...',0,1);const[s2,j2]=await api('/api/apply',{});note(s2===200?'applied':(j2.error||'error'),s2!==200)}
function build(){let h='<tr><th>PARAMETER</th><th id="ha"></th><th id="hb"></th><th></th></tr>';
 for(const[k,l,u,mn,mx,st]of ST.params){const f=d=>`<div class="sl"><input type="range" min="${mn}" max="${mx}" step="${st}" data-rd="${d}" data-rk="${k}"><input type="number" step="any" min="0" data-d="${d}" data-k="${k}"><span class="pm"><button type="button" class="st" data-bd="${d}" data-bk="${k}" data-dir="-1" title="-${st}">&minus;</button><button type="button" class="st" data-bd="${d}" data-bk="${k}" data-dir="1" title="+${st}">+</button></span></div>`;
  h+=`<tr><td>${l}</td><td>${f('a2b')}</td><td>${f('b2a')}</td><td class="mut">${u}</td></tr>`}
 $('wan').innerHTML=h;
 $('wan').addEventListener('input',e=>{const el=e.target,d=el.dataset.d||el.dataset.rd,k=el.dataset.k||el.dataset.rk;if(!k)return;
  sync(d,k,el.value);if($('link').checked)sync(d==='a2b'?'b2a':'a2b',k,el.value)});
 $('wan').addEventListener('click',e=>{const b=e.target.closest('button.st');if(!b||b.disabled)return;
  const d=b.dataset.bd,k=b.dataset.bk,p=ST.params.find(x=>x[0]===k),n=document.querySelector(`[data-d="${d}"][data-k="${k}"]`);
  let v=(parseFloat(n.value)||0)+(+b.dataset.dir)*p[5];v=Math.min(p[4],Math.max(p[3],v));n.value=parseFloat(v.toFixed(4));
  n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}))});
 $('wan').addEventListener('change',e=>{if(!(e.target.dataset.k||e.target.dataset.rk))return;
  for(const d of['a2b','b2a']){const g=k=>parseFloat(document.querySelector(`[data-d="${d}"][data-k="${k}"]`).value)||0;
   if(g('jitter')>0&&g('delay')<=0){sync(d,'delay',g('jitter'));note('delay set to '+g('jitter')+' ms (jitter needs a delay)')}
   if(g('reorder')>0&&g('delay')<ST.reorder_min_delay){sync(d,'delay',ST.reorder_min_delay);note('delay raised to '+ST.reorder_min_delay+' ms (reorder needs a delay)')}}
  liveApply()});
 for(const id of['mode','pa','pb','si'])$(id).addEventListener('change',heads);
 for(const id of['lldp','offl'])$(id).addEventListener('change',liveApply)}
function heads(){const m=$('mode').value;
 document.querySelectorAll('.m-bridge').forEach(e=>e.classList.toggle('hide',m!=='bridge'));
 document.querySelectorAll('.m-single').forEach(e=>e.classList.toggle('hide',m!=='single'));
 if(m==='single'){const d=$('si').value||'IF';$('ha').textContent='OUT (egress '+d+')';$('hb').textContent='IN (ingress '+d+' via IFB)'}
 else{const a=$('pa').value||'A',b=$('pb').value||'B';$('ha').textContent=a+' -> '+b;$('hb').textContent=b+' -> '+a}
 document.querySelectorAll('[data-d="b2a"][data-k="mtu"],[data-rd="b2a"][data-rk="mtu"],[data-bd="b2a"][data-bk="mtu"]').forEach(e=>e.disabled=(m==='single'))}
function fill(){const c=ST.cfg,b=c.bridge,names0=ST.ifaces.map(i=>i.name);
 const sel=(id,cur)=>{const n=names0.slice();if(cur&&!n.includes(cur))n.push(cur);const ph=$(id).dataset.ph;
  $(id).innerHTML=`<option value="">${ph}: -- SELECT --</option>`+n.map(x=>`<option value="${x}" ${x===cur?'selected':''}>${ph}: ${x}</option>`).join('')};
 sel('pa',b.port_a);sel('pb',b.port_b);sel('si',c.single.iface);$('mode').value=c.mode;
 $('bn').value=b.name;$('lldp').checked=b.lldp;$('offl').checked=b.offloads_off;
 document.querySelectorAll('[data-k]').forEach(el=>{el.value=c[el.dataset.d][el.dataset.k]});document.querySelectorAll('[data-rk]').forEach(el=>{el.value=c[el.dataset.rd][el.dataset.rk]});
 document.querySelectorAll('[data-k="mtu"],[data-rk="mtu"]').forEach(el=>{const d=el.dataset.d||el.dataset.rd;if(!+el.value)el.value=ST.default_mtu[d==='a2b'?0:1]});
 $('pre').innerHTML='<option value="" selected disabled hidden>LOAD PRESET</option>'+ST.presets.map(p=>`<option>${p}</option>`).join('');heads()}
function form(){const o={mode:$('mode').value,bridge:{name:$('bn').value,port_a:$('pa').value,port_b:$('pb').value,lldp:$('lldp').checked,offloads_off:$('offl').checked},
 single:{iface:$('si').value},a2b:{},b2a:{}};
 document.querySelectorAll('[data-k]').forEach(el=>{o[el.dataset.d][el.dataset.k]=el.value===''?(el.dataset.k==='burst'?1:0):el.value});
 const dm=ST?ST.default_mtu:[1500,1500];['a2b','b2a'].forEach((d,i)=>{if(+o[d].mtu===dm[i])o[d].mtu=0});return o}
function live(){const s=ST;
 $('sMode').textContent=s.mode==='single'?('SINGLE '+(s.cfg.single.iface||'-')):('BRIDGE '+s.cfg.bridge.name);
 $('ss').textContent=(s.active&&!s.stopped)?'STOP':'START';$('ss').className=(s.active&&!s.stopped)?'danger':'';
 $('sStateW').className=(s.active&&!s.stopped)?'u':'d';$('sState').textContent=s.stopped?'STOPPED - NO TRAFFIC':(s.active?'ACTIVE':'NOT ACTIVE');
 $('sCfg').textContent=(s.saved?'SAVED ':(s.has_file?'MODIFIED ':'NOT SAVED '))+(s.has_file?s.cfg_file:'');
 if(s.busy){note(s.busy+' ...',0,1);BUSY=1}else if(BUSY){BUSY=0;if($('msg').classList.contains('pending'))note('')}
 $('deps').hidden=!s.missing.length;$('miss').textContent=s.missing.join(', ');
 let h='<tr><th>INTERFACE</th><th>STATE</th><th>SPEED</th><th>MTU</th><th>MEMBER OF</th><th>ADDRESSES</th><th>MAC</th></tr>';
 for(const i of s.ifaces){const up=String(i.state).toUpperCase()==='UP';
  h+=`<tr><td>${i.name}</td><td class="state ${up?'up':'down'}">${String(i.state).toUpperCase()}</td><td>${i.speed>0?i.speed+' Mbit':'-'}</td><td>${i.mtu||'-'}</td><td>${i.master||'-'}</td><td>${i.addrs.join(' ')||'-'}</td><td>${i.mac}</td></tr>`}
 $('ifs').innerHTML=h;
 const st=s.stats,dt=PREV?s.ts-PREV.ts:0,r=(n,o,k)=>(n&&o&&dt>0)?Math.max(0,(n[k]-o[k])*8/dt):null,P=PREV?PREV.st:{};
 const f=v=>v===null?'-':fb(v),n=x=>x?x.packets:'-',d=x=>x?x.drops:'-',bl=x=>x?x.backlog:'-',o=x=>x?x.opts.replace(/["{}]/g,'').replace(/,/g,' '):'-';
 let a2i,a2o,b2i,b2o;
 if(s.mode==='single'){a2i=null;a2o=r(st.a,P.a,'tx_bytes');b2i=r(st.a,P.a,'rx_bytes');b2o=null}
 else{a2i=r(st.a,P.a,'rx_bytes');a2o=r(st.b,P.b,'tx_bytes');b2i=r(st.b,P.b,'rx_bytes');b2o=r(st.a,P.a,'tx_bytes')}
 const out=(i,x)=>`<td class="${x===null?'':(x>0?'up':(i!==null&&i>100000&&!s.stopped?'down bold':'mut'))}">${f(x)}</td>`;
 const row=(lab,i,x,q)=>`<tr><td>${lab}</td><td>${f(i)}</td>${out(i,x)}<td>${n(q)}</td><td class="${q&&q.drops>0?'down':''}">${d(q)}</td><td class="${q&&q.backlog>0?'down':''}">${bl(q)}</td></tr>`;
 $('live').innerHTML='<tr><th>DIRECTION</th><th>IN</th><th>OUT</th><th>NETEM PACKETS</th><th>NETEM DROPS</th><th>BACKLOG</th></tr>'
  +row(s.labels[0],a2i,a2o,st.a2b)+row(s.labels[1],b2i,b2o,st.b2a);
 PREV={ts:s.ts,st:st}}
let HIST=[],HLAST=0,LASTN='';
function nice(v){v=Math.max(v,1000);const e=Math.pow(10,Math.floor(Math.log10(v)));for(const m of[1,2,5,10])if(v<=m*e)return m*e}
function cpuShow(c){const e=$('cpu');if(!c||c.tot===undefined){e.textContent='';return}
 const f=(v)=>`<b class="${v>80?'hi':''}">${v} %</b>`;e.innerHTML=`CPU ${f(c.tot)} &nbsp; BUSIEST CORE ${f(c.max)} &nbsp; WANSIME ${f(c.proc)}`}
async function resetG(){const[s]=await api('/api/histreset','POST',{});HIST=[];HLAST=0;drawG();if(s===200)histRefresh()}
async function histRefresh(){const[s,j]=await api('/api/hist?since='+HLAST);if(s!==200||!j.hist)return;
 for(const r of j.hist){HIST.push(r);HLAST=r[0]}if(HIST.length>1800)HIST=HIST.slice(-1800);drawG()}
function drawG(){if(!ST||!HIST.length)return;const rng=+$('rng').value,now=HIST[HIST.length-1][0],t0=now-rng,vis=HIST.filter(r=>r[0]>=t0),
 cs=getComputedStyle(document.documentElement),col=n=>cs.getPropertyValue(n).trim(),dpr=window.devicePixelRatio||1;
 for(let d=0;d<2;d++){const cv=$('g'+d),W=cv.clientWidth,H=cv.clientHeight;if(!W)continue;cv.width=W*dpr;cv.height=H*dpr;
  const g=cv.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,W,H);
  const I=vis.map(r=>r[1+2*d]),O=vis.map(r=>r[2+2*d]),sc=nice(Math.max(1,...I,...O)),B=16,T=6,R=6,ph=H-T-B;
  g.font='11px ui-monospace,Menlo,Consolas,monospace';const yl=[0,1,2,3,4].map(k=>fAx(sc*(4-k)/4)),L=Math.ceil(Math.max(...yl.map(t=>g.measureText(t).width)))+14,pw=W-L-R;
  const X=t=>L+(t-t0)/rng*pw,Y=v=>T+ph-(v/sc)*ph;
  g.font='11px ui-monospace,Menlo,Consolas,monospace';g.textBaseline='middle';
  for(let k=0;k<=4;k++){const y=T+ph*k/4;g.strokeStyle=col('--line');g.beginPath();g.moveTo(L,y);g.lineTo(W-R,y);g.stroke();
   g.fillStyle=col('--dim');g.textAlign='right';g.fillText(yl[k],L-6,y)}
  g.textAlign='center';g.textBaseline='alphabetic';g.fillStyle=col('--dim');
  g.fillText('-'+(rng>=120?rng/60+' min':rng+' s'),L+14,H-3);g.fillText('now',W-R-12,H-3);
  const area=(arr,c,a)=>{if(!arr.length)return;g.beginPath();g.moveTo(X(vis[0][0]),Y(0));arr.forEach((v,i)=>g.lineTo(X(vis[i][0]),Y(v)));
   g.lineTo(X(vis[vis.length-1][0]),Y(0));g.closePath();g.globalAlpha=a;g.fillStyle=c;g.fill();g.globalAlpha=1;
   g.beginPath();arr.forEach((v,i)=>{i?g.lineTo(X(vis[i][0]),Y(v)):g.moveTo(X(vis[i][0]),Y(v))});g.strokeStyle=c;g.lineWidth=1.3;g.stroke()};
  area(I,col('--acc'),.18);area(O,col('--up'),.28);
  const ci=I.length?I[I.length-1]:0,co=O.length?O[O.length-1]:0,pk=Math.max(0,...HIST.map(r=>r[1+2*d])),tt=(ST.totals||[0,0,0,0]);
  $('t'+d).innerHTML=`<b>${(ST.labels||['',''])[d]}</b> &nbsp; <span class="i">IN ${fb(ci)}</span> &nbsp; <span class="o">OUT ${fb(co)}</span> &nbsp; TOP ${fb(pk)}<br>TOTAL IN ${fByt(tt[2*d])} / OUT ${fByt(tt[2*d+1])}`}}
function fAx(v){for(const[l,u]of[[1e9,'Gbit/s'],[1e6,'Mbit/s'],[1e3,'kbit/s']])if(v>=l)return(+(v/l).toFixed(2))+' '+u;return Math.round(v)+' bit/s'}
function fByt(n){for(const u of['B','KiB','MiB','GiB'])if(n<1024)return(u==='B'?n.toFixed(0):n.toFixed(2))+' '+u;else n/=1024;return n.toFixed(2)+' TiB'}
window.addEventListener('resize',drawG);
async function refresh(full){const[s,j]=await api('/api/state');if(s!==200)return;ST=j;histRefresh();cpuShow(j.cpu);if(j.notice&&j.notice!==LASTN){LASTN=j.notice;note(j.notice,1)}if(!$('wan').innerHTML)build();if(full)fill();live()}
function startStop(){if(ST&&ST.active&&!ST.stopped)act('/api/stop',{},1);else act('/api/apply')}
async function rescan(){await api('/api/cfg',form());await refresh(true)}
async function act(path,extra,nocfg){
 note('working ...',0,1);
 if(!nocfg){const[s0,j0]=await api('/api/cfg',form());if(s0!==200){note(j0.error||'error',1);return}}
 let[s,j]=await api(path,extra||{});
 if(s===409&&confirm(j.warn+'\n\nContinue anyway?')){note('working ...',0,1);[s,j]=await api(path,{...(extra||{}),force:true})}
 if(s===200)note(j.msg||'OK');else note(j.error||j.warn||'error',1);
 await refresh(true)}
function clock(){const n=new Date(),p=x=>String(x).padStart(2,'0');$('clock').textContent=`${p(n.getDate())}/${p(n.getMonth()+1)}/${n.getFullYear()} ${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`}
clock();setInterval(clock,1000);$('rng').addEventListener('change',drawG);
refresh(true);setInterval(()=>refresh(false),2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "wansim"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def _auth(self):
        auth = getattr(self.server, "auth", None)
        if not auth:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if hmac.compare_digest(base64.b64decode(h[6:]).decode(), auth):
                    return True
            except Exception:
                pass
        self._send(b"authentication required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="wansim"'})
        return False

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/":
            page = HTML.replace("@VERSION@", VERSION).replace("@CREDIT@", CREDIT)
            self._send(page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            try:
                self._json(self.server.sim.state())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif self.path.startswith("/api/hist"):
            try:
                since = float(self.path.split("since=", 1)[1]) if "since=" in self.path else 0.0
            except ValueError:
                since = 0.0
            self._json({"hist": self.server.sim.history(since, 600 if since <= 0 else 1800)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._json({"error": "application/json required"}, 415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        sim, p, client = self.server.sim, self.path, self.client_address[0]
        try:
            if p == "/api/cfg":
                sim.set_cfg(body)
                msg = "ok"
            elif p == "/api/apply":
                msg = sim.apply(bool(body.get("force")), client)
            elif p == "/api/clear":
                msg = sim.clear()
            elif p == "/api/preset":
                msg = sim.preset(body.get("name", ""))
            elif p == "/api/recover":
                msg = sim.recover()
            elif p == "/api/histreset":
                msg = sim.reset_hist()
            elif p == "/api/stop":
                msg = sim.stop()
            elif p == "/api/restore":
                msg = sim.restore()
            elif p == "/api/save":
                msg = sim.save()
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"msg": msg})
        except Warn as e:
            self._json({"warn": str(e)}, 409)
        except SimError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def start_web(sim, bind, port, auth):
    try:
        httpd = ThreadingHTTPServer((bind, port), Handler)
    except OSError as e:
        return None, f"Web GUI not started: {e}"
    httpd.sim, httpd.auth = sim, auth
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ips = local_ips()
    return httpd, f"Web GUI: http://{ips[0] if ips else socket.gethostname()}:{port}/" + ("  (auth on)" if auth else "")


# ----------------------------------------------------------------------------- TUI
def put(scr, y, x, s, attr=0):
    if not attr & curses.A_COLOR:
        attr |= curses.color_pair(1)  # white on black, like eping.py
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w - 1:
        try:
            scr.addstr(y, x, s[:w - x - 1], attr)
        except curses.error:
            pass


def bar(key, v, n=8):
    lo, hi, _st = SLIDER[key]
    f = 0 if hi <= lo else max(0, min(n, round((v - lo) / (hi - lo) * n)))
    return "[" + "#" * f + "-" * (n - f) + "]"


def prompt(scr, text, default=""):
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    curses.echo()
    scr.timeout(-1)
    scr.move(h - 1, 0)
    scr.clrtoeol()
    label = f"{text} [{default}]: "
    put(scr, h - 1, 0, label)
    try:
        s = scr.getstr(h - 1, min(len(label), w - 2), 40).decode().strip()
    except curses.error:
        s = ""
    curses.noecho()
    curses.curs_set(0)
    scr.timeout(500)
    return s or None


def ask(scr, text, default_yes=False):
    """y/n question on the last row; with default_yes Enter means yes ([Y/n]), otherwise only y"""
    h, w = scr.getmaxyx()
    scr.timeout(-1)
    scr.move(h - 1, 0)
    scr.clrtoeol()
    put(scr, h - 1, 0, text + (" [Y/n] " if default_yes else " (y/n) "), curses.A_BOLD)
    k = scr.getch()
    scr.timeout(500)
    if default_yes and k in (10, 13, curses.KEY_ENTER):
        return True
    return k in (ord("y"), ord("Y"))


def menu(scr, title, labels, cur=0):
    scr.timeout(-1)
    try:
        while True:
            scr.erase()
            h, w = scr.getmaxyx()
            put(scr, 0, max(0, (w - len(title)) // 2), title, curses.A_BOLD)
            put(scr, 1, 0, "-" * (w - 1), curses.A_BOLD)
            for i, lab in enumerate(labels):
                put(scr, 3 + i, 2, ("> " if i == cur else "  ") + lab, curses.A_REVERSE if i == cur else 0)
            put(scr, h - 2, 0, "-" * (w - 1), curses.A_BOLD)
            put(scr, h - 1, 1, "[UP/DOWN]=SELECT  [ENTER]=OK  [ESC]=CANCEL", curses.A_BOLD)
            k = scr.getch()
            if k == curses.KEY_UP:
                cur = (cur - 1) % len(labels)
            elif k == curses.KEY_DOWN:
                cur = (cur + 1) % len(labels)
            elif k in (10, 13, curses.KEY_ENTER):
                return cur
            elif k in (27, ord("q")):
                return None
    finally:
        scr.timeout(500)


def _tui(scr, sim, webmsg):
    curses.curs_set(0)
    scr.keypad(True)
    scr.timeout(500)
    curses.start_color()
    # same colour pairs as eping.py: 1 white, 2 green, 3 red - all on black
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_RED, curses.COLOR_BLACK)
    scr.bkgd(" ", curses.color_pair(1))
    WH, GN, RD = (curses.color_pair(i) for i in (1, 2, 3))
    BD = curses.A_BOLD
    scr.timeout(500)
    row = col = 0
    pend = 0.0    # time when changed values get applied (changes are collected for a short moment)
    sel = False   # SPACE: value selected, cursor keys then change it like +/-
    msg = ""
    msg_seen = ""
    msg_until = 0.0
    sx = [0]  # x position behind the status fields, where messages appear
    prev = None

    def bg(fn, *args):
        """run a system operation in the background so the screen stays alive"""
        res = {}

        def worker():
            try:
                res["v"] = fn(*args)
            except BaseException as e:
                res["e"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        n = 0
        while t.is_alive():
            hh, ww = scr.getmaxyx()
            put(scr, hh - 1, sx[0], ("WORKING " + "|/-\\"[n % 4]).ljust(max(0, ww - sx[0] - 1)), GN | BD)
            scr.refresh()
            n += 1
            time.sleep(0.15)
        if "e" in res:
            raise res["e"]
        return res.get("v")

    def do_apply():
        try:
            return bg(sim.apply)
        except Warn as e:
            return bg(sim.apply, True) if ask(scr, f"{e} Continue?") else "Cancelled"

    def iface_labels(ifs):
        return [f"{i['name']:<14} {i['state']:<8} {str(i['speed']) + ' Mbit' if i['speed'] > 0 else '-':<10} "
                f"{' '.join(i['addrs'][:2])}" for i in ifs]

    def setup_menu(st):
        ifs = st["ifaces"]
        m = menu(scr, "Mode", ["Bridge: transparent L2 bridge between two ports (A <-> B)",
                               "Single interface: impair local traffic (OUT egress, IN ingress via IFB)"],
                 0 if st["mode"] == "bridge" else 1)
        if m is None:
            return ""
        if not ifs or (m == 0 and len(ifs) < 2):
            return "Not enough Ethernet interfaces found"
        labels = iface_labels(ifs)
        if m == 1:
            i = menu(scr, "Interface", labels)
            if i is None:
                return ""
            sim.set_cfg({"mode": "single", "single": {"iface": ifs[i]["name"]}})
        else:
            ia = menu(scr, "Port A (e.g. site / LAN side = upstream source)", labels)
            if ia is None:
                return ""
            ib = menu(scr, "Port B (e.g. WAN side)", labels, min(1, len(labels) - 1))
            if ib is None:
                return ""
            name = prompt(scr, "Bridge name", st["cfg"]["bridge"]["name"]) or st["cfg"]["bridge"]["name"]
            off = ask(scr, "Disable NIC offloads gro/gso/tso/lro on the ports? (recommended)", True)
            sim.set_cfg({"mode": "bridge", "bridge": {"name": name, "port_a": ifs[ia]["name"], "port_b": ifs[ib]["name"],
                                                      "offloads_off": off}})
        return do_apply() if ask(scr, "Set up now and apply?", True) else "Setup selected (press a to apply)"

    def quit_ask(st):
        """quit questions: save config? reset system? (Enter = yes); returns True when the TUI should end"""
        if not st["saved"] and ask(scr, "Config not saved - save before quitting?"):
            sim.save()
        if st["active"] and ask(scr, "Reset system (restore original state)? - n = leave everything as it is", True):
            put(scr, h_[0] - 1, 0, " " * (w_[0] - 1))
            put(scr, h_[0] - 1, 1, "RESTORING ORIGINAL STATE ...", GN | BD)
            scr.refresh()
            try:
                sim.restore()
            except (SimError, Warn) as e:
                nonlocal_msg[0] = f"ERROR: {e}"
                return False                # stay in the TUI and show the error
        return True

    h_, w_, nonlocal_msg = [0], [0], [""]
    lastnote = ""
    while True:
        st = sim.state()
        c = st["cfg"]
        if st.get("notice") and st["notice"] != lastnote:
            lastnote = msg = st["notice"]
        h, w = scr.getmaxyx()
        h_[0], w_[0] = h, w
        if nonlocal_msg[0]:
            msg, nonlocal_msg[0] = nonlocal_msg[0], ""
        scr.erase()
        now = time.time()

        def hline(y):
            if 0 <= y < h - 1:
                put(scr, y, 0, "-" * (w - 1), WH | BD)

        # table adapts to the terminal width: two value columns share what is left of the label column
        cw = max(25, (w - 30) // 2)
        c1, c2 = 28, 28 + cw
        bw = min(120, max(8, cw - 17))      # slider bar width
        tw = cw - 2                        # text width inside a column

        def frame(y):
            for x in (0, 27, 27 + cw):
                put(scr, y, x, "|", WH | BD)
            put(scr, y, 28 + 2 * cw, "|", WH | BD)

        put(scr, 0, 1, time.strftime("%d/%m/%Y %H:%M:%S"), WH | BD)
        title = f"{PROG} version {VERSION} by Ewald Jeitler"
        put(scr, 0, max(0, (w - len(title)) // 2), title, WH | BD)
        if webmsg and len(title) + len(webmsg) + 26 < w:
            put(scr, 0, w - len(webmsg) - 2, webmsg, WH)
        hline(1)
        la, lb = st["labels"]
        frame(2)
        put(scr, 2, 2, "PARAMETER", WH | BD)
        put(scr, 2, c1 + 1, la[:tw], WH | BD)
        put(scr, 2, c2 + 1, lb[:tw], WH | BD)
        hline(3)
        for i, (key, label, *_r) in enumerate(PARAMS):
            y = 4 + i
            frame(y)
            put(scr, y, 2, label[:25], WH | BD)
            for cc, d in enumerate(("a2b", "b2a")):
                v = c[d][key]
                if key == "mtu" and not v:
                    v = st["default_mtu"][cc]
                dflt = st["default_mtu"][cc] if key == "mtu" else DEF_DIR[key]
                txt = fmt_val(key, v)
                if i == row and cc == col:
                    attr = (GN | BD | curses.A_REVERSE) if sel else (WH | curses.A_REVERSE)
                else:
                    attr = (GN | BD) if v != dflt else WH
                put(scr, y, 28 + cc * cw, (f" {bar(key, v, bw)} {txt}" + (" <+/->" if sel and i == row and cc == col else "")).ljust(cw - 1)[:cw - 1], attr)
        y0 = 4 + len(PARAMS)
        hline(y0)
        frame(y0 + 1)
        put(scr, y0 + 1, 2, "LIVE", WH | BD)
        put(scr, y0 + 1, c1 + 1, "IN / OUT", WH | BD)
        put(scr, y0 + 1, c2 + 1, "PKTS / DROPS / QUEUE", WH | BD)
        hline(y0 + 2)
        s = st["stats"]

        def rate(cur, old, k):
            if not cur or not old or not prev:
                return None
            dt = now - prev["t"]
            return max(0, (cur[k] - old[k]) * 8 / dt) if dt > 0 else None

        po = prev or {}
        if c["mode"] == "single":
            r = [(None, rate(s["a"], po.get("a"), "tx_bytes")), (rate(s["a"], po.get("a"), "rx_bytes"), None)]
        else:
            r = [(rate(s["a"], po.get("a"), "rx_bytes"), rate(s["b"], po.get("b"), "tx_bytes")),
                 (rate(s["b"], po.get("b"), "rx_bytes"), rate(s["a"], po.get("a"), "tx_bytes"))]
        for k, (nk, lab) in enumerate((("a2b", "OUT" if c["mode"] == "single" else la),
                                       ("b2a", "IN" if c["mode"] == "single" else lb))):
            ri, ro = r[k]
            fi = fmt_bps(ri) if ri is not None else "-"
            fo = fmt_bps(ro) if ro is not None else "-"
            n = s[nk]
            yy = y0 + 3 + k
            frame(yy)
            put(scr, yy, 2, lab, WH | BD)
            stall = (not st["stopped"]) and ri is not None and ro is not None and ri > 100000 and ro == 0  # traffic in, nothing out (expected while STOPPED)
            lv = f"{fi} / {fo}"
            if len(lv) > tw:
                lv = lv.replace("bit/s", "b/s")
            put(scr, yy, c1 + 1, lv[:tw], (RD | BD) if stall else WH)
            if n:
                put(scr, yy, c2 + 1, f"{n['packets']} / {n['drops']} / {n['backlog']}"[:tw],
                    (RD | BD) if (n["drops"] or n["backlog"]) else WH)
            else:
                put(scr, yy, c2 + 1, "-")
        prev = {"t": now, "a": s["a"], "b": s["b"]}

        # ---- big terminals: INTERFACES table and btop-like traffic graphs below the live table
        ycur, endy = y0 + 5, h - 2
        ifs = st.get("ifaces") or []
        xe = 27 + 2 * cw
        if ifs and endy - ycur >= 5:
            hline(ycur)
            addr_w = max(18, min(60, (w - 3) - 58 - 18))
            cols = (2, 17, 26, 38, 46, 58, 58 + addr_w)
            frame_e = lambda yy: (put(scr, yy, 0, "|", WH | BD), put(scr, yy, xe + 1, "|", WH | BD))
            frame_e(ycur + 1)
            for x, t in zip(cols, ("INTERFACE", "STATE", "SPEED", "MTU", "MEMBER OF", "ADDRESSES", "MAC")):
                put(scr, ycur + 1, x, t, WH | BD)
            hline(ycur + 2)
            nrows = max(1, min(len(ifs), endy - (ycur + 3)))
            for gi, ifc in enumerate(ifs[:nrows]):
                yy = ycur + 3 + gi
                frame_e(yy)
                up = str(ifc["state"]).upper() == "UP"
                vals = (ifc["name"], str(ifc["state"]).upper(), f"{ifc['speed']} Mbit" if ifc["speed"] and ifc["speed"] > 0 else "-",
                        str(ifc["mtu"] or "-"), ifc.get("master") or "-", " ".join(ifc["addrs"]) or "-", ifc["mac"])
                for x, t, a in zip(cols, vals, (WH, (GN | BD) if up else (RD | BD), WH, WH, WH, WH, WH)):
                    put(scr, yy, x, str(t)[:(58 + addr_w - x - 1) if x == 58 else 40], a)
            ycur += 3 + nrows
        histf = getattr(sim, "history", None)
        if histf and endy - ycur >= 9:
            hs = histf(0)
            hline(ycur)
            rr = endy - (ycur + 1)
            ph = rr // 2
            box = 34 if w >= 110 else 0
            gw = (w - 2) - box - 1
            tot = st.get("totals") or [0, 0, 0, 0]
            utf = "utf" in (locale.getpreferredencoding(False) or "").lower()
            if ph >= 4 and gw >= 20:
                for d in range(2):
                    py = ycur + 1 + d * ph
                    ph_d = ph if d == 0 else rr - ph  # the second graph takes the odd row
                    ins = [r[1 + 2 * d] for r in hs]
                    outs = [r[2 + 2 * d] for r in hs]
                    vis_i, vis_o = ins[-2 * gw:], outs[-2 * gw:]
                    scale = nice_scale(max(vis_i + vis_o + [0]))
                    ci, co = (ins[-1] if ins else 0), (outs[-1] if outs else 0)
                    put(scr, py, 2, st["labels"][d], WH | BD)
                    put(scr, py, 2 + len(st["labels"][d]) + 2, f"scale {fmt_bps(scale)}  (white = IN offered, green = OUT passed)", WH)
                    for gy, grow in enumerate(graph_rows(vis_i, vis_o, gw, ph_d - 1, scale, utf)):
                        g0 = 0
                        while g0 < len(grow):
                            g1 = g0
                            while g1 < len(grow) and grow[g1][1] == grow[g0][1]:
                                g1 += 1
                            put(scr, py + 1 + gy, 1 + g0, "".join(ch for ch, _p in grow[g0:g1]), (GN | BD) if grow[g0][1] else WH)
                            g0 = g1
                    if box:
                        bx = w - 1 - box
                        lines = [("IN    " + fmt_bps(ci), WH), ("OUT   " + fmt_bps(co), GN | BD),
                                 ("TOP   " + fmt_bps(max(ins + [0])), WH),
                                 ("TOTAL IN   " + fmt_bytes(tot[2 * d]), WH), ("TOTAL OUT  " + fmt_bytes(tot[2 * d + 1]), WH)]
                        for gy in range(min(len(lines), ph_d - 1)):
                            put(scr, py + 1 + gy, bx, "|", WH | BD)
                            put(scr, py + 1 + gy, bx + 2, lines[gy][0][:box - 4], lines[gy][1])
                for gy in range(ycur + 1, endy):  # left and right frame lines down to the key bar
                    put(scr, gy, 0, "|", WH | BD)
                    put(scr, gy, xe + 1, "|", WH | BD)

        # status line (bottom) - messages appear behind the fixed fields for a short time
        cfgs = "SAVED" if st["saved"] else ("MODIFIED" if st["has_file"] else "NOT SAVED")
        mode_txt = (f"SINGLE {c['single']['iface'] or '-'}" if c["mode"] == "single" else f"BRIDGE {c['bridge']['name']}")
        parts = [("MODE: ", WH | BD), (mode_txt, WH | BD), (" | STATE: ", WH | BD),
                 ("STOPPED - NO TRAFFIC", RD | BD) if st["stopped"] else (("ACTIVE", GN | BD) if st["active"] else ("NOT ACTIVE", RD | BD)),
                 (" | CONFIG: ", WH | BD), (cfgs, (GN | BD) if st["saved"] else (WH | BD)),
                 (f" | MIRROR: {'ON' if sim.link else 'OFF'} |", WH | BD)]
        x = 1
        for text, attr in parts:
            put(scr, h - 1, x, text, attr)
            x += len(text)
        sx[0] = x + 1
        if msg != msg_seen:
            msg_seen = msg
            msg_until = now + (8.0 if msg.startswith("ERROR") else 3.0)
        if st["busy"]:
            put(scr, h - 1, sx[0], ("WORKING: " + st["busy"]).upper(), GN | BD)
        elif msg and now < msg_until:
            put(scr, h - 1, sx[0], msg, (RD | BD) if msg.startswith("ERROR") else (GN | BD))
        if w < 80 or h < y0 + 8:
            tts = " | TERMINAL TOO SMALL "
            put(scr, h - 1, max(0, w - len(tts) - 1), tts, RD | curses.A_BLINK)
        if st["missing"]:
            put(scr, h - 3, 1, "MISSING: " + ", ".join(st["missing"]).upper() + "  - RUN: SUDO ./WANSIME.PY --INSTALL", RD | BD)

        # key bar - on the dashed line, three label sets so it fits narrow terminals; active toggles in green
        hline(h - 2)
        ap = "STOP" if (st["active"] and not st["stopped"]) else "START"
        sp = ("[SPACE]=SELECT/LEAVE" if sel else "[SPACE]=SELECT")
        keys_xl = [(" [B]=SETUP ", 0), (f" [A]={ap} ", 0), (f" {sp} ", sel), (" [ARROWS]=MOVE ", 0), (" [E]=EDIT ", 0), (" [+/-]=STEP ", 0), (" [P]=PRESET ", 0), (" [L]=MIRROR ", sim.link), (" [C]=RESET ", 0), (" [S]=SAVE CONFIG ", 0), (" [F]=FIX USB ", 0),
                   (" [R]=RESTORE ", 0), (" [Q]=QUIT ", 0)]
        keys_full = [(" [B]=SETUP ", 0), (f" [A]={ap} ", 0), (" [SPACE]=SEL ", sel), (" [E]=EDIT ", 0), (" [+/-]=STEP ", 0), (" [P]=PRESET ", 0), (" [L]=MIRROR ", sim.link), (" [C]=RESET ", 0), (" [S]=SAVE ", 0), (" [F]=FIX ", 0),
                     (" [R]=RESTORE ", 0), (" [Q]=QUIT ", 0)]
        keys_short = [(" [B]=SETUP ", 0), (f" [A]={ap} ", 0), (" [SPC]SEL ", sel), (" [E]=EDIT ", 0), (" [P]=PRESET ", 0),
                      (" [L]=MIRROR ", sim.link), (" [C]=RESET ", 0), (" [S]=SAVE ", 0),
                      (" [F]=FIX ", 0), (" [R]=RESTORE ", 0), (" [Q]=QUIT ", 0)]
        keys_tiny = [(" [B]SETUP ", 0), (f" [A]{ap} ", 0), (" [SPC]SEL ", sel), (" [E]EDIT ", 0), (" [P]PRE ", 0), (" [L]MIR ", sim.link),
                     (" [C]RST ", 0), (" [S]SAVE ", 0), (" [F]FIX ", 0), (" [R]RESTORE ", 0), (" [Q]QUIT ", 0)]
        keys_min = [(" B ", 0), (f" A:{ap} ", 0), (" SPC ", sel), (" E ", 0), (" P ", 0), (" L ", sim.link), (" C ", 0), (" S ", 0), (" F ", 0), (" R ", 0), (" Q ", 0)]
        keys = keys_min
        for cand in (keys_xl, keys_full, keys_short, keys_tiny, keys_min):
            if sum(len(t) for t, _a in cand) + 2 <= w:
                keys = cand
                break
        # spread leftover width evenly between the entries so the bar fills the terminal
        total = sum(len(t) for t, _a in keys)
        gap = min(4, max(0, (w - 3 - total) // max(1, len(keys) - 1)))
        kx = 2
        for text, on in keys:
            put(scr, h - 2, kx, text, (GN | BD) if on else WH)
            kx += len(text) + gap
        scr.refresh()

        scr.timeout(150 if pend else 500)
        try:
            k = scr.getch()
        except KeyboardInterrupt:          # Ctrl-C asks the same quit questions
            if quit_ask(st):
                return
            continue
        if 65 <= k <= 90:                  # Caps Lock / shift: treat letters like lower case
            k += 32
        if pend and time.time() >= pend and k not in (ord("a"), ord("c"), ord("p"), ord("r"), ord("q"), ord("b"), ord("f")):
            pend = 0.0                     # changes stopped for a moment: apply them once
            try:
                msg = bg(sim.apply_now)
            except (SimError, Warn) as e:
                msg = f"ERROR: {e}"
        if k == -1:
            continue
        if k in (ord("a"), ord("c"), ord("p"), ord("r"), ord("q"), ord("b"), ord("f")):
            pend = 0.0                     # these apply (or discard) everything themselves
        if pend:
            msg = "Value set - applying in a moment"
        else:
            msg = ""
        try:
            if k == ord(" "):
                sel = not sel
                msg = "Selected: UP/DOWN = other value, LEFT/RIGHT = change (PgUp/PgDn x10), TAB = other column - SPACE to leave" if sel else ""
            elif sel and k in (10, 13, curses.KEY_ENTER, 27):
                sel = False
            elif sel and k == curses.KEY_UP:
                row = (row - 1) % len(PARAMS)
            elif sel and k == curses.KEY_DOWN:
                row = (row + 1) % len(PARAMS)
            elif sel and k == 9:
                col = 1 - col
            elif sel and k in (curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_PPAGE, curses.KEY_NPAGE):
                d = "a2b" if col == 0 else "b2a"
                key, label, unit, step, lo, hi = PARAMS[row]
                sign = 1 if k in (curses.KEY_RIGHT, curses.KEY_PPAGE) else -1
                mult = 10 if k in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
                base = c[d][key] or (st["default_mtu"][col] if key == "mtu" else 0)
                new = round(min(hi, max(lo, base + sign * step * mult)), 4)
                if key == "mtu" and new == st["default_mtu"][col]:
                    new = 0
                msg = sim.set_value(d, key, new, apply=False)
                pend = time.time() + 0.6
            elif k == curses.KEY_UP:
                row = (row - 1) % len(PARAMS)
            elif k == curses.KEY_DOWN:
                row = (row + 1) % len(PARAMS)
            elif k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                col = 1 - col
            elif k in (10, 13, curses.KEY_ENTER, ord("e")):
                d = "a2b" if col == 0 else "b2a"
                key, label, unit, *_r = PARAMS[row]
                cur = c[d][key] or (st["default_mtu"][col] if key == "mtu" else 0)
                t = prompt(scr, f"{label} {d} ({unit})", f"{cur}k" if key == "rate" else f"{cur:g}")
                if t is None:
                    continue
                new = parse_rate(t) if key == "rate" else (int(float(t)) if key == "burst" else float(t))
                if key == "mtu" and new == st["default_mtu"][col]:
                    new = 0
                pend = 0.0
                msg = bg(sim.set_value, d, key, new)
            elif k in (ord("+"), ord("="), ord("-"), ord("["), ord("]")):
                d = "a2b" if col == 0 else "b2a"
                key, label, unit, step, lo, hi = PARAMS[row]
                sign = 1 if k in (ord("+"), ord("="), ord("]")) else -1
                mult = 10 if k in (ord("["), ord("]")) else 1
                base = c[d][key] or (st["default_mtu"][col] if key == "mtu" else 0)
                new = round(min(hi, max(lo, base + sign * step * mult)), 4)
                if key == "mtu" and new == st["default_mtu"][col]:
                    new = 0
                msg = sim.set_value(d, key, new, apply=False)
                pend = time.time() + 0.6
            elif k == ord("l"):
                sim.link = not sim.link
            elif k == ord("p"):
                names = list(PRESETS)
                i = menu(scr, "Preset (A->B / OUT = upstream, B->A / IN = downstream)", names)
                if i is not None:
                    msg = bg(sim.preset, names[i])
            elif k == ord("a"):
                if st["active"] and not st["stopped"]:
                    msg = bg(sim.stop)
                else:
                    msg = do_apply()
            elif k == ord("c"):
                msg = bg(sim.clear)
            elif k == ord("s"):
                msg = sim.save()
            elif k == ord("b"):
                msg = setup_menu(st)
            elif k == ord("r"):
                if ask(scr, "Restore original state (remove bridge / IFB)?"):
                    msg = bg(sim.restore)
            elif k == ord("f"):
                if ask(scr, "Re-initialize the USB adapter(s) of the active setup?"):
                    msg = bg(sim.recover)
            elif k == ord("q"):
                if quit_ask(st):
                    return
        except (SimError, ValueError) as e:
            msg = f"ERROR: {e}"
        except Warn as e:
            msg = f"ERROR: {e}"


# ----------------------------------------------------------------------------- main
def _stat_fields(pid):
    st = read_text(f"/proc/{pid}/stat")
    if ")" not in st:
        return None
    return st.rsplit(")", 1)[1].split()  # [state, ppid, ...]


def pid_alive(pid):
    f = _stat_fields(pid)
    return bool(f) and f[0] not in ("Z", "X")


def find_instances():
    """other running copies of this script (also under its former name), never our own parent chain"""
    anc, pid = set(), os.getpid()
    while pid > 1 and pid not in anc:
        anc.add(pid)
        f = _stat_fields(pid)
        pid = int(f[1]) if f else 0
    res = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in anc:
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        a0 = os.path.basename(argv[0])
        if not (a0.startswith("python") or a0 == "sudo"):
            continue
        if not any(os.path.basename(a) in SCRIPT_NAMES for a in argv[1:4]):
            continue
        f = _stat_fields(d)
        if not f or f[0] in ("Z", "X"):
            continue
        try:
            tty = os.readlink(f"/proc/{d}/fd/0").replace("/dev/", "")
        except OSError:
            tty = "?"
        res.append({"pid": int(d), "tty": tty, "state": f[0], "cmd": " ".join(argv)[:60]})
    return sorted(res, key=lambda i: i["pid"])


def kill_instances(inst):
    if os.path.exists(UNIT_FILE) and run(["systemctl", "is-active", "--quiet", "wansime.service"])[0] == 0:
        print("Stopping systemd service wansime.service ...")
        run(["systemctl", "stop", "wansime.service"], timeout=20)
    pids = [i["pid"] for i in inst]
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        alive = [p for p in pids if pid_alive(p)]
        if not alive:
            break
        for p in alive:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        end = time.time() + wait
        while time.time() < end and any(pid_alive(p) for p in alive):
            time.sleep(0.2)
    left = [p for p in pids if pid_alive(p)]
    if left:
        raise SimError("Could not stop PID " + ", ".join(map(str, left)) + " (stuck in the kernel?) - a reboot may be needed")
    print("Old process(es) stopped. The network state is unchanged (use --restore to reset it).")


def handle_instances(kill_old, interactive):
    inst = find_instances()
    if not inst:
        return
    print(f"Another {PROG} is already running:")
    for i in inst:
        print(f"  PID {i['pid']:<7} {i['tty']:<8} state {i['state']}  {i['cmd']}")
    if not kill_old:
        if not interactive:
            raise SimError("another instance is running - stop it or start with --kill-old")
        if input("[k] kill the old process(es) and start    [a] abort (default): ").strip().lower() not in ("k", "kill"):
            sys.exit("Aborted - nothing started.")
    kill_instances(inst)


def status_text(sim):
    st = sim.state()
    c = st["cfg"]
    head = f"single interface {c['single']['iface']}" if c["mode"] == "single" else \
        f"bridge {c['bridge']['name']} ({c['bridge']['port_a']} <-> {c['bridge']['port_b']})"
    lines = [f"{PROG} {VERSION}  {CREDIT}", f"{head}: {'ACTIVE' if st['active'] else 'not active'}"]
    for d, lab in zip(("a2b", "b2a"), st["labels"]):
        vals = ", ".join(f"{k}={fmt_val(k, v)}" for k, v in c[d].items() if v != DEF_DIR[k])
        lines.append(f"  {lab}: {vals or 'no impairment'}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(prog=PROG, description="WAN simulator (tc netem): bridge or single-interface mode, "
                                 f"TUI + web GUI (port {WEB_PORT}).", epilog=CREDIT)
    ap.add_argument("--daemon", action="store_true", help="headless, web GUI only (default when no terminal)")
    ap.add_argument("--no-web", action="store_true", help="TUI without web GUI")
    ap.add_argument("--port", type=int, default=WEB_PORT, help=f"web port (default {WEB_PORT})")
    ap.add_argument("--bind", default="0.0.0.0", help="web bind address (default 0.0.0.0)")
    ap.add_argument("--auth", metavar="USER:PASS", default=os.environ.get("WANSIM_AUTH"),
                    help="HTTP basic auth for the web GUI (or env WANSIM_AUTH)")
    ap.add_argument("--install", action="store_true", help="install required packages / kernel modules")
    ap.add_argument("--install-service", action="store_true", help="install and start systemd service wansime")
    ap.add_argument("--remove-service", action="store_true", help="stop and remove systemd service")
    ap.add_argument("--apply", action="store_true", help="apply saved config once and exit")
    ap.add_argument("--restore", action="store_true", help="restore original state and exit")
    ap.add_argument("--status", action="store_true", help="show current state and exit")
    ap.add_argument("--all-ifaces", action="store_true", help="list virtual Ethernet interfaces too")
    ap.add_argument("--kill-old", action="store_true", help="stop other running instances without asking")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args()
    if args.version:
        print(f"{PROG} {VERSION}  {CREDIT}")
        return 0
    if os.geteuid() != 0:
        # not root: restart ourselves through sudo, which asks for the password
        sudo = shutil.which("sudo")
        if sudo and sys.stdin.isatty():
            print(f"{PROG}: root privileges required - sudo will ask for the password")
            try:
                os.execv(sudo, [sudo, sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:])
            except OSError as e:
                print(f"{PROG}: cannot start sudo: {e}", file=sys.stderr)
        print(f"{PROG}: root privileges required - run with sudo", file=sys.stderr)
        return 1
    try:
        if args.install:
            install_deps(print)
            return 0
        if args.install_service:
            print(install_service())
            return 0
        if args.remove_service:
            print(remove_service())
            return 0
        if not args.status:
            handle_instances(args.kill_old, sys.stdin.isatty())
        sim = Sim(args.all_ifaces)
        daemon = args.daemon or not sys.stdin.isatty()
        if sim.missing and not (args.status or args.restore):
            print("Missing requirements: " + ", ".join(sim.missing))
            if daemon:
                print(f"Install them with: sudo {PROG} --install", file=sys.stderr)
                return 1
            try:
                answer = input("Install now? [Y/n] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer in ("", "y", "j"):
                install_deps(print)
                sim.reload_deps()
                if sim.missing:
                    print("Still missing: " + ", ".join(sim.missing), file=sys.stderr)
                    return 1
            else:
                print(f"Not installed - later: sudo {PROG} --install")
                return 1
        if args.status:
            print(status_text(sim))
            return 0
        if args.restore:
            print(sim.restore())
            return 0
        if args.apply:
            print(sim.apply(force=True))
            return 0
        if daemon:
            httpd, info = start_web(sim, args.bind, args.port, args.auth)
            print(f"{PROG} {VERSION} {CREDIT}\n{info}", flush=True)
            if httpd is None:
                return 1
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
            return 0
        info = "Web GUI: disabled"
        if not args.no_web:
            _h, info = start_web(sim, args.bind, args.port, args.auth)
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass
        curses.wrapper(_tui, sim, info)
        print(THX)
        return 0
    except SimError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nInterrupted - {THX}")
        return 130
    except Warn as e:
        print(f"WARNING: {e} - use the TUI/web GUI to confirm", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

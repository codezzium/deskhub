"""Connect 3D printers: Bambu Lab (cloud account or LAN) and network printers of other brands.

    uv run deskhub-printers [--config path/to/config.toml] [list|add-cloud|add-local|add-<type>|remove]
"""
import argparse
import functools
import getpass
import pathlib
import re

import tomlkit

from . import bambu_cloud as cloud
from . import printer_poll as poll
from . import printer_types as ptypes
from .__main__ import find_config
from .calendar_feed import _safe_error
from .calendars_cli import ask, load, write
from .printer import MAX_PRINTERS, MODELS, lan_key, probe_local

_IP = re.compile(r"[\w.\-:]+")


def printers_of(doc) -> tomlkit.items.AoT:
    # Removing the last printer leaves `printers = []`, an inline array that tables can't be
    # appended to (the file would no longer parse), so start a fresh [[printers]] list then.
    if not isinstance(doc.get("printers"), tomlkit.items.AoT) and not doc.get("printers"):
        doc["printers"] = tomlkit.aot()
    return doc["printers"]


def _keyring():
    import keyring
    return keyring


def _ensure_page(doc):
    """With an explicit [pages] list, the printer page must be in it to show up."""
    pages = doc.get("pages")
    enabled = pages.get("enabled") if pages is not None else None
    if enabled is not None and "printer" not in enabled:
        enabled.append("printer")
        print("Added \"printer\" to [pages] enabled so the page shows.")


def _same(a, b) -> bool:
    if a.get("type") in ("local", "cloud") and b.get("type") in ("local", "cloud"):
        return a.get("serial") == b.get("serial")
    if a.get("type") != b.get("type"):
        return False
    spec = _spec(str(a.get("type", "")))
    port = spec.default_port if spec else 0
    return (a.get("host"), int(a.get("port", port))) == (b.get("host"), int(b.get("port", port)))


def _add(path, doc, entry: dict) -> bool:
    printers = printers_of(doc)
    if any(_same(p, entry) for p in printers):
        print(f"{entry.get('serial') or entry.get('host')} is already configured.")
        return False
    if len(printers) >= MAX_PRINTERS:
        print(f"The board shows at most {MAX_PRINTERS} printers. Remove one first.")
        return False
    row = tomlkit.table()
    for k, v in entry.items():
        row[k] = v
    printers.append(row)
    _ensure_page(doc)
    write(path, doc)
    return True


def _spec(kind: str):
    try:
        return ptypes.module(kind).SPEC if kind in ptypes.BACKENDS else None
    except ModuleNotFoundError:
        return None


def describe(p) -> str:
    spec = _spec(str(p.get("type", "")))
    if spec:
        brand = spec.brand or spec.type
        return (f"{p.get('name', '')}  [{p.get('model') or spec.default_model}, {brand} "
                f"{p.get('host', '')}:{p.get('port', spec.default_port)}]")
    model = p.get("model") or MODELS.get(str(p.get("serial", ""))[:3], "")
    where = f"LAN {p.get('host', '')}" if p.get("type") == "local" else f"cloud {p.get('account', '')}"
    return f"{p.get('name', '')}  [{model + ', ' if model else ''}{where}, serial {p.get('serial', '')}]"


def cmd_list(path):
    printers = printers_of(load(path))
    if not printers:
        print("No printers configured yet.")
    for i, p in enumerate(printers, 1):
        print(f"  {i}. {describe(p)}")


def sign_in(http, region: str, email: str) -> str | None:
    """Password, then an emailed code if Bambu asks for one. -> access token or None."""
    password = getpass.getpass("Bambu password (hidden): ")
    if not password:
        return None
    try:
        res = cloud.login(http, region, email, password)
        if res["status"] == "code":
            cloud.send_code(http, region, email)
            print(f"Bambu emailed a verification code to {email}.")
            code = ask("Code (empty to cancel)")
            if not code:
                return None
            return cloud.login_with_code(http, region, email, code)
        return res["token"]
    except cloud.BambuError as e:
        print(f"Sign-in failed: {_safe_error(e, password)}")
        return None


def cmd_add_cloud(path):
    print("""
Bambu cloud
  Sign in with your Bambu Lab account to read your printers' status through Bambu's servers.
  This works from anywhere, and OrcaSlicer and Bambu Handy keep working as usual.
  The access token (valid about 3 months) goes into your OS credential store; your password
  is not saved.
""")
    email = ask("Bambu account email (empty to cancel)")
    if not email:
        return
    region = ask("Region, us or cn (cn only for China accounts)", "us").lower()
    region = "cn" if region == "cn" else "us"
    with cloud.make_client() as http:
        token = sign_in(http, region, email)
        if not token:
            return
        try:
            found = cloud.list_printers(http, region, token)
        except cloud.BambuError as e:
            print(f"Could not list your printers: {_safe_error(e, token)}")
            return
    if not found:
        print("Signed in, but no printers are bound to this account.")
        return
    print("Printers on this account:")
    for i, d in enumerate(found, 1):
        print(f"  {i}. {d['name']}  ({d['model'] or 'unknown model'}, {'online' if d['online'] else 'offline'})")
    pick = ask("Numbers to add, comma separated, or 'all'", "all")
    try:
        chosen = found if pick.lower() == "all" else \
            [found[int(n) - 1] for n in pick.replace(" ", "").split(",") if n]
    except (ValueError, IndexError):
        print("Didn't understand that selection.")
        return
    _keyring().set_password(cloud.SERVICE, email, token)
    doc = load(path)
    added = 0
    for d in chosen:
        entry = {"name": d["name"], "type": "cloud", "serial": d["serial"],
                 "account": email, "region": region}
        if d["model"]:
            entry["model"] = d["model"]
        added += _add(path, doc, entry)
    print(f"Token saved to the credential store, {added} printer(s) saved to {path}.")
    print("Restart the companion to pick it up.")


def cmd_add_local(path):
    print("""
Local (LAN) printer
  Needs, from the printer's screen: the IP address and the 8-character access code
  (Settings > WLAN/Network, or Settings > LAN Only Mode); the serial number is under
  Settings > Device (or on the printer's label).
  Newer firmware only allows third-party tools when LAN Only Mode or Developer Mode is on.
""")
    host = ask("Printer IP address (empty to cancel)")
    if not host:
        return
    if not _IP.fullmatch(host):
        print("That doesn't look like an IP address.")
        return
    serial = ask("Serial number").upper()
    if not serial:
        return
    code = getpass.getpass("Access code (hidden): ").strip()
    if not code:
        return
    name = ask("Name for this printer", MODELS.get(serial[:3], "Printer"))
    print("Testing the connection (up to 10 s)...")
    try:
        ok, msg = probe_local(host, serial, code)
    except Exception as e:
        ok, msg = False, f"Connection test failed: {_safe_error(e, code)}"
    print(msg)
    if not ok and ask("Save it anyway? (y/n)", "n").lower() != "y":
        return
    _keyring().set_password(cloud.SERVICE, lan_key(serial), code)
    if _add(path, load(path), {"name": name, "type": "local", "serial": serial, "host": host}):
        print(f"Access code saved to the credential store, printer saved to {path}.")
        print("Restart the companion to pick it up.")


def cmd_add_polled(path, spec):
    """Add a printer of any network-polled brand, asking what its BackendSpec lists."""
    print("\n" + spec.intro + "\n")
    host = ask("Printer IP address or hostname (empty to cancel)")
    if not host:
        return
    if not _IP.fullmatch(host):
        print("That doesn't look like an IP address or hostname.")
        return
    port = spec.default_port
    if spec.ask_port:
        port_s = ask("Port", str(spec.default_port))
        if not port_s.isdigit() or not 0 < int(port_s) < 65536:
            print("The port is a number from 1 to 65535.")
            return
        port = int(port_s)
    entry = {"name": "", "type": spec.type, "host": host}
    if port != spec.default_port:
        entry["port"] = port
    if any(_same(p, entry) for p in printers_of(load(path))):
        # Before the connection test, which can put a prompt on the printer's screen.
        print(f"{host} is already configured.")
        return
    for field in spec.fields:
        value = ask(field.prompt, field.default)
        if not value and field.required:
            return
        if value:
            entry[field.key] = value
    secret = None
    if spec.secret_prompt:
        secret = getpass.getpass(f"{spec.secret_prompt}: ").strip() or None
        if spec.secret_required and not secret:
            return
    print("Testing the connection...")
    try:
        res = spec.probe(dict(entry), secret)
    except Exception as e:
        res = poll.ProbeResult(False, f"Connection test failed: {_safe_error(e, secret or '')}")
    print(res.message)
    if not res.ok and ask("Save it anyway? (y/n)", "n").lower() != "y":
        return
    secret = res.secret or secret
    entry["name"] = ask("Name for this printer", res.name or res.model or spec.default_model)
    model = ask("Model, shown on the board", res.model or spec.default_model)
    if model and model != spec.default_model:
        entry["model"] = model
    if _add(path, load(path), entry):
        if secret:
            _keyring().set_password(poll.SERVICE, poll.secret_key(spec.type, host, port), secret)
            print("Credential saved to the credential store.")
        print(f"Printer saved to {path}. Restart the companion to pick it up.")


def cmd_remove(path):
    doc = load(path)
    printers = printers_of(doc)
    if not printers:
        print("Nothing to remove.")
        return
    cmd_list(path)
    n = ask("Number to remove (empty to cancel)")
    if not n.isdigit() or not 1 <= int(n) <= len(printers):
        return
    gone = printers[int(n) - 1]
    del printers[int(n) - 1]
    write(path, doc)
    print("Removed.")
    spec = _spec(str(gone.get("type", "")))
    if spec:
        host, port = str(gone.get("host", "")), int(gone.get("port", spec.default_port))
        service, key = poll.SERVICE, poll.secret_key(spec.type, host, port)
        try:
            if _keyring().get_password(service, key) is None:
                return
        except Exception:
            return
    elif gone.get("type") == "local":
        service, key = cloud.SERVICE, lan_key(str(gone.get("serial", "")))
    elif not any(p.get("account") == gone.get("account") for p in printers):
        service, key = cloud.SERVICE, str(gone.get("account", ""))
    else:
        return
    if ask("Also delete the stored credential? (y/n)", "y").lower() == "y":
        kr = _keyring()
        try:
            kr.delete_password(service, key)
        except kr.errors.PasswordDeleteError:
            pass


def _commands() -> dict:
    """name -> (menu label, function of path)."""
    cmds = {"list": ("List printers", cmd_list),
            "add-cloud": ("Bambu Lab: add printers from a Bambu cloud account", cmd_add_cloud),
            "add-local": ("Bambu Lab: add a printer on the local network (LAN)", cmd_add_local)}
    for kind in ptypes.available():
        spec = ptypes.module(kind).SPEC
        cmds[f"add-{kind}"] = (spec.menu, functools.partial(cmd_add_polled, spec=spec))
    cmds["remove"] = ("Remove a printer", cmd_remove)
    return cmds


def main():
    cmds = _commands()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("command", nargs="?", choices=list(cmds))
    args = ap.parse_args()
    path = find_config(args.config)
    if args.command:
        cmds[args.command][1](path)
        return
    menu(path)


def menu(path: pathlib.Path):
    """Interactive printer menu; also used by `deskhub-setup`."""
    cmds = list(_commands().values())
    while True:
        print(f"\nPrinters in {path}")
        for i, (label, _) in enumerate(cmds, 1):
            print(f"  {i:>2}. {label}")
        choice = ask(f"Choose 1-{len(cmds)}, or empty to quit")
        if not choice:
            return
        if choice.isdigit() and 1 <= int(choice) <= len(cmds):
            cmds[int(choice) - 1][1](path)


if __name__ == "__main__":
    main()

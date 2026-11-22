import argparse
import json
import sys
from pathlib import Path
from typing import Optional, List

from .core import UploadQueue
from .transport import resolve_auth_token


def _print(obj) -> None:
	print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
	root = Path(args.root).expanduser().resolve()
	q = UploadQueue(root=root, vault_config_path=Path(args.config) if args.config else None)
	_print(q.describe(probe_server=args.probe_server))
	return 0


def cmd_upload(args: argparse.Namespace) -> int:
	root = Path(args.root).expanduser().resolve()
	q = UploadQueue(root=root, vault_config_path=Path(args.config) if args.config else None)
	if args.all or args.max_files is not None:
		# Without `--all`, `rua upload` is structurally single-file -- it calls
		# run_once, which uploads exactly one file and returns -- so draining a
		# day's recordings would mean re-invoking the CLI per file and paying the
		# ~0.47 s Python start cost every time. `--all` drives run_many, which is
		# where file-level concurrency lives, so a backlog drains in one process
		# with one set of channels.
		# max_files=0 == "everything pending" (see UploadQueue.run_many).
		res = q.run_many(max_files=args.max_files if args.max_files is not None else 0)
	else:
		res = q.run_once()
	_print(res)
	return upload_exit_status(res)


def upload_exit_status(result: dict) -> int:
	"""0 for success/no work/deferred recordings; 1 for any file failure."""
	return 1 if result.get("errors") or result.get("error") else 0


def _is_loopback(host: Optional[str]) -> bool:
	"""True for the in-compose/dev topologies where ingest is on this box."""
	return (host or "").strip().lower() in {"localhost", "127.0.0.1", "::1", "ingest", ""}


def cmd_config_validate(args: argparse.Namespace) -> int:
	from robot_vault_config.model import load_config
	try:
		cfg = load_config(Path(args.config)) if args.config else load_config(None)
		auth = getattr(cfg, "auth", None)
		token = resolve_auth_token(token_file=getattr(auth, "token_file", None))
		# A credential is required whenever TLS is on and the server is off-box,
		# not only under mTLS. An enrolled robot has mtls=false and still must
		# present its per-robot `ignr1.` token -- which is exactly the enrolled-robot (token profile) topology.
		if cfg.tls.enabled and token is None:
			if cfg.tls.mtls or not _is_loopback(cfg.server.host):
				raise ValueError(
					"a credential is required for an off-box TLS robot; set "
					"auth.token_file (the enrolled path) or INGEST_AUTH_TOKEN"
				)
		_print(cfg.to_dict())
		return 0
	except (OSError, ValueError) as exc:
		print(json.dumps({"error": str(exc), "command": "config validate"}), file=sys.stderr)
		return 2


def build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(prog="rua", description="Robot Upload Agent")
	sub = p.add_subparsers(dest="cmd", required=True)

	sp = sub.add_parser("status", help="Show queue status")
	sp.add_argument("--root", required=True)
	sp.add_argument("--config", default=None, help="Path to shared Vault config (JSON)")
	sp.add_argument(
		"--probe-server",
		action="store_true",
		help="query ingest's advertised stream cap (used by benchmark preflight)",
	)
	sp.set_defaults(func=cmd_status)

	sp = sub.add_parser("upload", help="Start/resume uploads (manual trigger)")
	sp.add_argument("--root", required=True)
	sp.add_argument("--config", default=None)
	sp.add_argument("--all", action="store_true",
	                help="drain every pending file in one process (file-level "
	                     "concurrency bounded by upload_agent.parallel_streams)")
	sp.add_argument("--max-files", type=int, default=None,
	                help="with --all: take on at most N files this invocation")
	sp.set_defaults(func=cmd_upload)

	sp = sub.add_parser("config", help="Config operations")
	sub2 = sp.add_subparsers(dest="subcmd", required=True)
	sp2 = sub2.add_parser("validate", help="Validate config file")
	sp2.add_argument("--config")
	sp2.set_defaults(func=cmd_config_validate)

	return p


def main(argv: Optional[List[str]] = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	return args.func(args)

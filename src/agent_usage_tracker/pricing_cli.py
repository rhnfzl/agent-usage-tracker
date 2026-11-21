"""Register the explicit local price refresh and optional sink load."""

from __future__ import annotations

import argparse
import os
import sys

from . import pricing
from .config import AutError


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("sync-prices", help="refresh the API-equivalent model price map")
    parser.add_argument("--out", help="output JSON (default: <config-dir>/model_prices.json)")
    parser.add_argument("--litellm-url", default=pricing.LITELLM_URL)
    parser.add_argument("--models-dev-url", default=pricing.MODELS_DEV_URL)
    parser.add_argument("--genai-prices-url", default=pricing.GENAI_PRICES_URL)
    parser.add_argument("--sink", action="store_true", help="also load rates using AUT_PRICE_PG_DSN")


def run(args: argparse.Namespace) -> None:
    out = args.out or pricing.default_price_path()
    dsn = os.environ.get("AUT_PRICE_PG_DSN") if args.sink else None
    if args.sink and not dsn:
        raise AutError("sync-prices --sink needs AUT_PRICE_PG_DSN")
    n = pricing.sync_prices(out, litellm_url=args.litellm_url, models_dev_url=args.models_dev_url,
                            genai_prices_url=args.genai_prices_url, pg_dsn=dsn)
    print(f"synced {n} model prices -> {out}", file=sys.stderr)
    if dsn:
        print(f"loaded {n} model prices into agents.model_price", file=sys.stderr)

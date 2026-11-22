"""
The call to order-manager (P2a, Amar 2026-11-22): one signal per SYNCHRONOUS invoke.

order-manager is the one common order manager and the only writer of algo.position_master.
This playbook hands it each step it must record and waits for the answer: applied, duplicate,
rejected_stored or ignored. Anything else - a refused invoke, a FunctionError (order-manager
raised), a missing answer - RAISES here, so this run fails loudly and Lambda's retry runs the
tick again; order-manager answers `duplicate` for whatever it already applied.

Its role needs lambda:InvokeFunction on order-manager's ARN only
(iam/strategy-range-liquidity-sweep-role.invoke-order-manager.json).
"""

import json
import logging

import boto3

from config import ORDER_MANAGER_CONTRACT_VERSION, ORDER_MANAGER_FUNCTION

logger = logging.getLogger()
_client = {}
OK = ("applied", "duplicate", "rejected_stored", "ignored")


def send(signal, client=None):
    lam = client or _client.setdefault("lambda", boto3.client("lambda"))
    payload = json.dumps({"contract_version": ORDER_MANAGER_CONTRACT_VERSION, "signal": signal}).encode()
    resp = lam.invoke(FunctionName=ORDER_MANAGER_FUNCTION, InvocationType="RequestResponse", Payload=payload)
    body = resp["Payload"].read()
    if resp.get("StatusCode") != 200 or resp.get("FunctionError"):
        raise RuntimeError(f"order-manager refused {signal['kind']} (entry {signal['entry_ts']}, candle "
                           f"{signal['candle_ts']}): StatusCode {resp.get('StatusCode')}, {resp.get('FunctionError')}: "
                           f"{body[:500]!r}")
    answer = json.loads(body)
    if not isinstance(answer, dict) or answer.get("result") not in OK:
        raise RuntimeError(f"order-manager gave no usable answer for {signal['kind']}: {body[:500]!r}")
    return answer

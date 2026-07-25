# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"


class PredictionMarketResolver(gl.Contract):
    markets: TreeMap[str, str]
    market_count: bigint
    bets: TreeMap[str, str]
    bet_count: bigint
    market_bet_ids: TreeMap[str, str]

    def __init__(self) -> None:
        self.market_count = bigint(0)
        self.bet_count = bigint(0)

    @gl.public.write
    def create_market(
        self,
        question: str,
        resolution_criteria: str,
        closes_at: str,
        evidence_url: str,
    ) -> None:
        # evidence_url is committed at market creation time, not chosen later by
        # whoever happens to trigger resolution, so it cannot be picked
        # adversarially after the outcome is already known.
        market_id = str(int(self.market_count))
        self.markets[market_id] = json.dumps({
            "id": market_id,
            "question": question,
            "resolution_criteria": resolution_criteria,
            "closes_at": closes_at,
            "evidence_url": evidence_url,
            "status": "OPEN",
            "yes_pool_cents": 0,
            "no_pool_cents": 0,
            "winning_side": None,
            "reasoning": "",
        })
        self.market_bet_ids[market_id] = ""
        self.market_count = bigint(int(self.market_count) + 1)

    @gl.public.write
    def place_bet(
        self,
        market_id: str,
        bettor: str,
        side: str,
        amount_usd: str,
    ) -> None:
        market_raw = self.markets.get(market_id, None)
        if market_raw is None:
            raise gl.vm.UserError(ERROR_EXPECTED + " Market not found")
        market = json.loads(market_raw)
        if market["status"] != "OPEN":
            raise gl.vm.UserError(ERROR_EXPECTED + " Market is not open for betting")

        side = side.strip().upper()
        if side not in ("YES", "NO"):
            raise gl.vm.UserError(ERROR_EXPECTED + " Side must be YES or NO")

        try:
            amount_cents = int(round(float(amount_usd) * 100))
        except (ValueError, TypeError):
            raise gl.vm.UserError(ERROR_EXPECTED + " Invalid amount_usd")
        if amount_cents <= 0:
            raise gl.vm.UserError(ERROR_EXPECTED + " amount_usd must be positive")

        bet_id = str(int(self.bet_count))
        self.bets[bet_id] = json.dumps({
            "id": bet_id,
            "market_id": market_id,
            "bettor": bettor,
            "side": side,
            "amount_cents": amount_cents,
            "payout_cents": 0,
        })
        self.bet_count = bigint(int(self.bet_count) + 1)

        existing = self.market_bet_ids.get(market_id, "")
        self.market_bet_ids[market_id] = (existing + "," + bet_id) if existing else bet_id

        if side == "YES":
            market["yes_pool_cents"] = int(market["yes_pool_cents"]) + amount_cents
        else:
            market["no_pool_cents"] = int(market["no_pool_cents"]) + amount_cents
        self.markets[market_id] = json.dumps(market)

    @gl.public.write
    def resolve_market(self, market_id: str, current_date: str) -> None:
        market_raw = self.markets.get(market_id, None)
        if market_raw is None:
            raise gl.vm.UserError(ERROR_EXPECTED + " Market not found")
        market = json.loads(market_raw)
        if market["status"] != "OPEN":
            raise gl.vm.UserError(ERROR_EXPECTED + " Market already resolved")
        if current_date < market["closes_at"]:
            raise gl.vm.UserError(ERROR_EXPECTED + " Market has not closed yet")

        question = market["question"]
        resolution_criteria = market["resolution_criteria"]
        evidence_url = market["evidence_url"]

        def resolve() -> str:
            # Contract-side acquisition of authoritative, time-relevant evidence:
            # every validator independently fetches the pre-committed source and
            # resolves against its actual content, instead of relying on the
            # model's own training-time knowledge of "real-world events."
            resp = gl.nondet.web.get(evidence_url)
            if resp.status >= 500:
                raise gl.vm.UserError(ERROR_TRANSIENT + " Evidence source temporarily unavailable")
            if resp.status >= 400:
                raise gl.vm.UserError(ERROR_EXTERNAL + " Evidence source returned status " + str(resp.status))
            evidence_content = resp.body.decode("utf-8", errors="replace")[:6000]

            task = (
                "You are an impartial oracle resolving a prediction market. You must base your\n"
                "decision on the FETCHED EVIDENCE below, not on your own general knowledge, since\n"
                "the evidence reflects the actual state of the world as of resolution time.\n\n"
                "QUESTION: " + question + "\n"
                "RESOLUTION CRITERIA: " + resolution_criteria + "\n"
                "MARKET CLOSE DATE: " + market["closes_at"] + "\n\n"
                "FETCHED EVIDENCE (from " + evidence_url + "):\n" + evidence_content + "\n\n"
                "Determine the outcome strictly according to the resolution criteria and what the\n"
                "fetched evidence actually shows.\n"
                "Return ONLY a JSON object:\n"
                "{\"outcome\": \"YES\", \"reasoning\": \"one sentence citing the evidence\"}\n\n"
                "Rules:\n"
                "- outcome must be exactly YES, NO, or UNRESOLVED\n"
                "- UNRESOLVED only if the fetched evidence genuinely does not address the question\n"
                "- reasoning: one sentence citing what the evidence showed\n"
                "Return ONLY the JSON, no other text."
            )
            raw = gl.nondet.exec_prompt(task)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                raise gl.vm.UserError(ERROR_LLM + " Non-JSON response from model")

            outcome = parsed.get("outcome", None)
            if outcome not in ("YES", "NO", "UNRESOLVED"):
                raise gl.vm.UserError(ERROR_LLM + " Invalid outcome: " + str(outcome))

            return json.dumps({
                "outcome": outcome,
                "reasoning": str(parsed.get("reasoning", "")),
            })

        result_str = gl.eq_principle.prompt_comparative(
            resolve,
            principle="The outcome field must match exactly between validators.",
        )
        result = json.loads(result_str)
        outcome = result["outcome"]

        yes_pool = int(market["yes_pool_cents"])
        no_pool = int(market["no_pool_cents"])
        total_pool = yes_pool + no_pool

        bet_ids_str = self.market_bet_ids.get(market_id, "")
        bet_ids = [b for b in bet_ids_str.split(",") if b]

        if outcome == "UNRESOLVED":
            market["status"] = "UNRESOLVED"
            for bid in bet_ids:
                bet = json.loads(self.bets[bid])
                bet["payout_cents"] = bet["amount_cents"]
                self.bets[bid] = json.dumps(bet)
        else:
            market["status"] = "RESOLVED_" + outcome
            winning_pool = yes_pool if outcome == "YES" else no_pool
            for bid in bet_ids:
                bet = json.loads(self.bets[bid])
                if bet["side"] == outcome and winning_pool > 0:
                    bet["payout_cents"] = (bet["amount_cents"] * total_pool) // winning_pool
                else:
                    bet["payout_cents"] = 0
                self.bets[bid] = json.dumps(bet)

        market["winning_side"] = outcome
        market["reasoning"] = result["reasoning"]
        self.markets[market_id] = json.dumps(market)

    @gl.public.view
    def get_market(self, market_id: str) -> str:
        data = self.markets.get(market_id, None)
        if data is None:
            return json.dumps({"error": "Market not found"})
        return data

    @gl.public.view
    def get_all_markets(self) -> str:
        all_markets = {}
        for i in range(int(self.market_count)):
            mid = str(i)
            all_markets[mid] = json.loads(self.markets.get(mid, "{}"))
        return json.dumps(all_markets)

    @gl.public.view
    def get_bet(self, bet_id: str) -> str:
        data = self.bets.get(bet_id, None)
        if data is None:
            return json.dumps({"error": "Bet not found"})
        return data

    @gl.public.view
    def get_market_bets(self, market_id: str) -> str:
        bet_ids_str = self.market_bet_ids.get(market_id, "")
        bet_ids = [b for b in bet_ids_str.split(",") if b]
        result = []
        for bid in bet_ids:
            data = self.bets.get(bid, None)
            if data is not None:
                result.append(json.loads(data))
        return json.dumps(result)

    @gl.public.view
    def get_total_market_count(self) -> bigint:
        return self.market_count

    @gl.public.view
    def get_total_bet_count(self) -> bigint:
        return self.bet_count

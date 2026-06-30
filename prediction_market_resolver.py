# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
import json


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
    ) -> None:
        market_id = str(int(self.market_count))
        self.markets[market_id] = json.dumps({
            "id": market_id,
            "question": question,
            "resolution_criteria": resolution_criteria,
            "closes_at": closes_at,
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
            raise gl.vm.UserError("[EXPECTED] Market not found")
        market = json.loads(market_raw)
        if market["status"] != "OPEN":
            raise gl.vm.UserError("[EXPECTED] Market is not open for betting")

        side = side.strip().upper()
        if side not in ("YES", "NO"):
            raise gl.vm.UserError("[EXPECTED] Side must be YES or NO")

        try:
            amount_cents = int(round(float(amount_usd) * 100))
        except (ValueError, TypeError):
            raise gl.vm.UserError("[EXPECTED] Invalid amount_usd")
        if amount_cents <= 0:
            raise gl.vm.UserError("[EXPECTED] amount_usd must be positive")

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
            raise gl.vm.UserError("[EXPECTED] Market not found")
        market = json.loads(market_raw)
        if market["status"] != "OPEN":
            raise gl.vm.UserError("[EXPECTED] Market already resolved")
        if current_date < market["closes_at"]:
            raise gl.vm.UserError("[EXPECTED] Market has not closed yet")

        question = market["question"]
        resolution_criteria = market["resolution_criteria"]

        def resolve() -> str:
            task = (
                "You are an impartial oracle resolving a prediction market based on real-world facts.\n\n"
                "QUESTION: " + question + "\n"
                "RESOLUTION CRITERIA: " + resolution_criteria + "\n"
                "MARKET CLOSE DATE: " + market["closes_at"] + "\n\n"
                "Determine the outcome using your knowledge of real-world events up to and including the close date.\n"
                "Return ONLY a JSON object:\n"
                "{\"outcome\": \"YES\", \"reasoning\": \"one sentence\"}\n\n"
                "Rules:\n"
                "- outcome must be exactly YES, NO, or UNRESOLVED\n"
                "- UNRESOLVED only if the outcome genuinely cannot be determined from available facts\n"
                "- reasoning: one sentence citing the basis for the decision\n"
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
            parsed = json.loads(raw)
            outcome = parsed.get("outcome", "UNRESOLVED")
            if outcome not in ("YES", "NO", "UNRESOLVED"):
                outcome = "UNRESOLVED"
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

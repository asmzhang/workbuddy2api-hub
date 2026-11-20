"""熔断放行 / 置0：清窗口必须连计数一起清，而且不许越界碰别的护栏。

Run with: python tests/run_all.py breaker_release
"""
import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="wb-breaker-release-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_accounts as A  # noqa: E402  (needs ACCOUNTS_DIR set first)
import wb_proxy  # noqa: E402

INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"


def account_dir():
    return os.environ["ACCOUNTS_DIR"]


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def jwt(iss=INTL_ISS, sub="u-1"):
    return "%s.%s.sig" % (
        b64({"alg": "none", "typ": "JWT"}),
        b64({"iss": iss, "sub": sub, "exp": 4102444800}),
    )


def write_credential(folder, uid):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"uid": uid, "domain": "www.workbuddy.ai", "realm": "intl",
                   "accessToken": jwt(sub=uid)}, fh)


def load_account(uid):
    """Give one account its own pool directory and return (pool, account).

    The pool loads `<dir>/*.json`, one credential file per account, so each of
    these tests gets a private folder: a shared directory would make one test's
    account visible to another's pool.
    """
    folder = os.path.join(account_dir(), uid)
    write_credential(folder, uid)
    pool = A.AccountPool(folder)
    pool.load()
    return pool, pool.get(uid)


def load_pool(*uids):
    """One pool holding several accounts, the way the proxy holds them."""
    folder = os.path.join(account_dir(), "pool-" + "-".join(uids))
    for uid in uids:
        write_credential(folder, uid)
    pool = A.AccountPool(folder)
    pool.load()
    return pool


def trip(account, count=None):
    """Fail the account enough times to trip the breaker."""
    for _ in range(count or A.BREAKER_THRESHOLD):
        account.note_failure("HTTP 502")
    return account


class ReleaseSemantics(unittest.TestCase):
    """What the button actually clears, and what it must leave alone."""

    def test_release_clears_window_and_count(self):
        pool, account = load_account("u-release-1")
        trip(account)
        view = account.public()
        self.assertTrue(view["breakerFor"], "连败到位就该有熔断窗口")
        self.assertEqual(view["fails"], A.BREAKER_THRESHOLD)
        self.assertTrue(account.clear_breaker())
        view = account.public()
        self.assertIsNone(view["breakerFor"], "放行之后不该还挂着熔断窗口")
        self.assertEqual(view["fails"], 0, "放行之后连败计数必须归零")
        self.assertEqual(account.last_fail_at, 0.0)

    def test_nothing_to_release_returns_false(self):
        pool, account = load_account("u-release-2")
        self.assertFalse(account.clear_breaker(), "没熔断也没计数的号不该谎称清掉了什么")
        self.assertIsNone(account.public()["breakerFor"])

    def test_count_only_release(self):
        """熔断窗口已经跑完、计数还挂着：这时「置0」就是全部意义。"""
        pool, account = load_account("u-release-3")
        account.fails = A.BREAKER_THRESHOLD + 2
        account.last_fail_at = time.time()
        self.assertIsNone(account.public()["breakerFor"])
        self.assertTrue(account.clear_breaker(), "只剩计数也是真有东西要清")
        self.assertEqual(account.public()["fails"], 0)

    def test_next_trip_after_release_starts_at_baseline(self):
        """放行真正的收益：下一次熔断回到基线窗口，而不是接着翻倍。"""
        pool, account = load_account("u-release-4")
        trip(account)
        account.clear_breaker()
        trip(account)
        self.assertTrue(A.breaker_backoff(A.BREAKER_THRESHOLD)
                        <= account.public()["breakerFor"]
                        <= A.breaker_backoff(A.BREAKER_THRESHOLD) + 1,
                        "放行后再熔断应该是基线窗口")

    def test_release_keeps_every_other_guard(self):
        pool, account = load_account("u-release-5")
        trip(account)
        now = time.time()
        # 熔断之外的护栏先摆上：放行之后它们必须原样在。
        account.cooldown_until = now + 777.0
        account.balance_until = now + 666.0
        account.degrade_until = now + 555.0
        account.degrade_count = 4
        account.soft_streak = 3
        account.account_banned = True
        account.banned_at = now - 9.0
        self.assertTrue(account.clear_breaker())
        view = account.public()
        self.assertIsNone(view["breakerFor"])
        self.assertAlmostEqual(account.cooldown_until, now + 777.0,
                               msg="放行不该顺手解除上游限流冷却")
        self.assertAlmostEqual(account.balance_until, now + 666.0,
                               msg="放行不该顺手解除积分不足")
        self.assertAlmostEqual(account.degrade_until, now + 555.0,
                               msg="放行不该顺手解除降权")
        self.assertEqual(account.degrade_count, 4, "降权计数不归放行管")
        self.assertEqual(account.soft_streak, 3, "软限流 streak 不归放行管")
        self.assertTrue(account.account_banned, "放行不等于解封")
        self.assertAlmostEqual(account.banned_at, now - 9.0)
        # 其它护栏还在，面板就该继续显示它们
        self.assertTrue(view["cooldownFor"], "冷却没清，面板就该看得见")
        self.assertTrue(view["degradeFor"], "降权没清，面板就该看得见")

    def test_release_is_visible_in_the_panel_view(self):
        """放行之后面板得立刻看不出这条护栏，否则按钮点了像没点。"""
        pool, account = load_account("u-release-6")
        trip(account)
        self.assertIsNotNone(account.public()["lastFailFor"], "触发过熔断就该有最近失败时刻")
        account.clear_breaker()
        view = account.public()
        self.assertIsNone(view["breakerFor"], "面板不该还挂着熔断窗口")
        self.assertEqual(view["fails"], 0, "面板不该还显示连败次数")
        self.assertIsNone(view["lastFailFor"], "连败计数清了，最近失败时刻也该回到「没有」")
        self.assertIsInstance(view["failWindow"], int)


class PoolRelease(unittest.TestCase):
    """池级入口：账号不存在返回 None，本来没事的号不落盘。"""

    def test_unknown_uid_returns_none(self):
        pool, _ = load_account("u-release-pool")
        self.assertIsNone(pool.clear_breaker("u-ghost"))

    def test_clean_account_is_not_saved(self):
        pool, account = load_account("u-release-pool-clean")
        with mock.patch.object(A.Account, "save", autospec=True) as save:
            view = pool.clear_breaker("u-release-pool-clean")
        self.assertIsNotNone(view)
        self.assertEqual(save.call_count, 0,
                         "没有熔断可清的号不该写盘：批量入口会把整屏账号送进来")

    def test_tripped_account_is_saved(self):
        pool, account = load_account("u-release-pool-hot")
        trip(account)
        with mock.patch.object(A.Account, "save", autospec=True) as save:
            view = pool.clear_breaker("u-release-pool-hot")
        self.assertEqual(save.call_count, 1, "真清掉了东西就该落盘一次")
        self.assertIsNone(view["breakerFor"])


class HandlerRoute(unittest.TestCase):
    """面板那条路：/accounts/clear-breaker 的 released / clean / missing 口径。"""

    def setUp(self):
        self.pool = load_pool("u-handler-hot", "u-handler-clean")
        trip(self.pool.get("u-handler-hot"))
        self.replies = []
        self.errors = []
        self.logs = []

        class FakeHandler(wb_proxy.Handler):
            def __init__(self):
                self.headers = {}
                self.wfile = io.BytesIO()
                self.path = "/accounts/clear-breaker"
                self.status = None
                self.sent = {}

            def send_response(self, code):
                self.status = code

            def send_header(self, *args, **kwargs):
                pass

            def end_headers(self):
                pass

        self.h = FakeHandler()
        self.h._json_panel = lambda code, obj: self.replies.append((code, obj))
        self.h._error = lambda code, msg, kind="server_error": self.errors.append((code, kind))
        patches = [
            mock.patch.object(wb_proxy, "POOL", self.pool),
            mock.patch.object(wb_proxy, "account_views", lambda *a, **k: []),
            mock.patch.object(wb_proxy, "log", lambda *a, **k: self.logs.append(a)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def call(self, payload):
        """Invoke the route and return the body it sent, whatever it returned.

        The real `_json_panel` returns None, so the route's own return value is
        not the body: read what the handler recorded instead of trusting it.
        """
        self.h._route_accounts_clear_breaker(payload)
        if self.errors:
            return None
        self.assertEqual(len(self.replies), 1, "该回一条 200，实际回了 %r" % (self.replies,))
        code, body = self.replies[0]
        self.assertEqual(code, 200)
        return body

    def test_single_uid_reports_a_release(self):
        body = self.call({"uid": "u-handler-hot"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["released"], 1)
        self.assertEqual(body["clean"], 0)
        self.assertEqual(body["missing"], [])
        self.assertTrue(self.logs, "放行该留一条日志：谁被放行、当时挂着多长的窗口")

    def test_clean_uid_counts_as_clean_not_released(self):
        body = self.call({"uid": "u-handler-clean"})
        self.assertEqual(body["released"], 0, "本来就没事的号不算放行，否则面板数字是虚报")
        self.assertEqual(body["clean"], 1)

    def test_batch_mixes_released_clean_and_missing(self):
        body = self.call({"uids": ["u-handler-hot", "u-handler-clean", "u-ghost"]})
        self.assertEqual((body["released"], body["clean"]), (1, 1))
        self.assertEqual(body["missing"], ["u-ghost"])

    def test_only_ghost_uids_is_404(self):
        self.call({"uids": ["u-ghost-1", "u-ghost-2"]})
        self.assertEqual([e[0] for e in self.errors], [404])

    def test_empty_uid_list_is_400(self):
        """空清单不是「没这个账号」，是一个什么都不做的请求。"""
        self.call({"uids": []})
        self.call({"uids": ["", "  "]})
        self.assertEqual([e[0] for e in self.errors], [400, 400])

    def test_duplicate_uids_count_once(self):
        """同一个号送两遍不该数出两个「已放行」—— 面板上的数字要能对上账号。"""
        body = self.call({"uids": ["u-handler-hot", "u-handler-hot", " u-handler-hot "]})
        self.assertEqual((body["released"], body["clean"]), (1, 0))

    def test_missing_payload_is_400(self):
        self.call({})
        self.call({"uid": ""})
        self.assertEqual([e[0] for e in self.errors], [400, 400])

    def test_non_list_uids_is_400(self):
        self.call({"uids": "u-handler-hot"})
        self.assertEqual([e[0] for e in self.errors], [400])


class PanelWiring(unittest.TestCase):
    """看板接线与三语词条：文案与词典必须逐字对得上。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "dashboard.html"), encoding="utf-8") as fh:
            self.html = fh.read()

    def test_row_and_toolbar_buttons_exist(self):
        self.assertIn('onclick="clearBreaker(&quot;', self.html, "账号行缺熔断放行按钮")
        self.assertIn('<button class="sec mini" onclick="clearBreaker(&quot;',
                      self.html, "行按钮不该造新样式")
        self.assertIn('id="btnBulkBreaker" onclick="clearBreakerBulk(this)"',
                      self.html, "号池工具栏缺批量放行按钮")
        self.assertIn("(a.breakerFor || a.fails", self.html,
                      "行按钮只在真有熔断或连败计数时出现")

    def test_js_posts_the_endpoint(self):
        self.assertIn("async function clearBreaker(uid, btn)", self.html)
        self.assertIn("async function clearBreakerBulk(btn)", self.html)
        self.assertEqual(self.html.count("'/accounts/clear-breaker'"), 2,
                         "单个与批量各走一次同一个端点")
        self.assertIn("acctScopeUids()", self.html.split("async function clearBreakerBulk")[1][:600],
                      "批量口径必须与启停/删除一致：有勾选按勾选，没勾选按当前筛选结果")

    def test_route_is_registered(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "wb_proxy.py"), encoding="utf-8") as fh:
            proxy = fh.read()
        self.assertIn('if path == "/accounts/clear-breaker":', proxy)
        self.assertIn("def _route_accounts_clear_breaker(self, payload):", proxy)

    def test_every_new_phrase_has_both_translation_tables(self):
        phrases = [
            ("熔断放行", "Release breaker", "熔斷放行"),
            ("熔断放行：结束该账号正在生效的熔断窗口，并把连续失败计数清零。只清这一条 "
             "—— 上游限流冷却、积分不足、降权、软限流 streak 和封禁都不动，它们各有各的解除入口。",
             "Release breaker: end the breaker window now in force on this account and zero "
             "its consecutive-failure count. This clears that one guard only — upstream "
             "rate-limit cooldowns, insufficient credits, demotion, soft-limit streaks and "
             "account bans all stay put, each with its own release path.",
             "熔斷放行：結束該帳號正在生效的熔斷視窗，並把連續失敗計數歸零。只清這一項 "
             "—— 上游限流冷卻、積分不足、降權、軟限流 streak 與封禁都不動，它們各有各的解除入口。"),
            ("有勾选时只放行勾选的账号，没有勾选时放行当前筛选结果里所有带熔断或连败计数的账号；"
             "只清熔断，不动冷却、降权、软限流和封禁",
             "With a selection this releases only the selected accounts; with none, it releases "
             "every account in the current filtered result that carries a breaker or a failure "
             "streak. Breaker only — cooldowns, demotion, soft limits and bans are left alone.",
             "有勾選時只放行勾選的帳號，沒有勾選時放行目前篩選結果裡所有帶熔斷或連敗計數的帳號；"
             "只清熔斷，不動冷卻、降權、軟限流與封禁"),
            ("熔断已放行", "Breaker released", "熔斷已放行"),
            ("熔断已放行 ", "Breaker released: ", "熔斷已放行 "),
            ("该账号没有熔断可放行", "This account has no breaker to release",
             "該帳號沒有熔斷可放行"),
            ("当前范围内的账号都没有熔断", "No account in the current scope has a breaker",
             "目前範圍內的帳號都沒有熔斷"),
            ("没有可操作的账号", "No accounts to act on", "沒有可操作的帳號"),
        ]
        for zh, en, hant in phrases:
            self.assertIn("'%s': '%s'," % (zh, en), self.html, "DICT 缺英文词条：" + zh)
            self.assertIn('"%s": "%s",' % (zh, hant), self.html, "HANT_PHRASES 缺正体词条：" + zh)


if __name__ == "__main__":
    unittest.main(verbosity=2)

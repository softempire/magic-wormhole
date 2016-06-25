from __future__ import print_function, unicode_literals
import json, itertools, time
from binascii import hexlify
import mock
from twisted.trial import unittest
from twisted.python import log
from twisted.internet import protocol, reactor, defer
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.endpoints import clientFromString, connectProtocol
from twisted.web import client
from autobahn.twisted import websocket
from .. import __version__
from .common import ServerBase
from ..server import rendezvous, transit_server
from ..server.rendezvous import Usage, SidedMessage
from ..server.database import get_db

class Server(ServerBase, unittest.TestCase):
    def test_apps(self):
        app1 = self._rendezvous.get_app("appid1")
        self.assertIdentical(app1, self._rendezvous.get_app("appid1"))
        app2 = self._rendezvous.get_app("appid2")
        self.assertNotIdentical(app1, app2)

    def test_nameplate_allocation(self):
        app = self._rendezvous.get_app("appid")
        nids = set()
        # this takes a second, and claims all the short-numbered nameplates
        def add():
            nameplate_id = app.allocate_nameplate("side1", 0)
            self.assertEqual(type(nameplate_id), type(""))
            nid = int(nameplate_id)
            nids.add(nid)
        for i in range(9): add()
        self.assertNotIn(0, nids)
        self.assertEqual(set(range(1,10)), nids)

        for i in range(100-10): add()
        self.assertEqual(len(nids), 99)
        self.assertEqual(set(range(1,100)), nids)

        for i in range(1000-100): add()
        self.assertEqual(len(nids), 999)
        self.assertEqual(set(range(1,1000)), nids)

        add()
        self.assertEqual(len(nids), 1000)
        biggest = max(nids)
        self.assert_(1000 <= biggest < 1000000, biggest)

    def _nameplate(self, app, name):
        np_row = app._db.execute("SELECT * FROM `nameplates`"
                                 " WHERE `app_id`='appid' AND `name`=?",
                                 (name,)).fetchone()
        if not np_row:
            return None, None
        npid = np_row["id"]
        side_rows = app._db.execute("SELECT * FROM `nameplate_sides`"
                                    " WHERE `nameplates_id`=?",
                                    (npid,)).fetchall()
        return np_row, side_rows

    def test_nameplate(self):
        app = self._rendezvous.get_app("appid")
        name = app.allocate_nameplate("side1", 0)
        self.assertEqual(type(name), type(""))
        nid = int(name)
        self.assert_(0 < nid < 10, nid)
        self.assertEqual(app.get_nameplate_ids(), set([name]))
        # allocate also does a claim
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["side"], "side1")
        self.assertEqual(side_rows[0]["added"], 0)

        # duplicate claims by the same side are combined
        mailbox_id = app.claim_nameplate(name, "side1", 1)
        self.assertEqual(type(mailbox_id), type(""))
        self.assertEqual(mailbox_id, np_row["mailbox_id"])
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["added"], 0)
        self.assertEqual(mailbox_id, np_row["mailbox_id"])

        # and they don't updated the 'added' time
        mailbox_id2 = app.claim_nameplate(name, "side1", 2)
        self.assertEqual(mailbox_id, mailbox_id2)
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["added"], 0)

        # claim by the second side is new
        mailbox_id3 = app.claim_nameplate(name, "side2", 3)
        self.assertEqual(mailbox_id, mailbox_id3)
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 2)
        self.assertEqual(sorted([row["side"] for row in side_rows]),
                         sorted(["side1", "side2"]))
        self.assertIn(("side2", 3),
                      [(row["side"], row["added"]) for row in side_rows])

        # a third claim marks the nameplate as "crowded", and adds a third
        # claim (which must be released later), but leaves the two existing
        # claims alone
        self.assertRaises(rendezvous.CrowdedError,
                          app.claim_nameplate, name, "side3", 4)
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 3)

        # releasing a non-existent nameplate is ignored
        app.release_nameplate(name+"not", "side4", 0)

        # releasing a side that never claimed the nameplate is ignored
        app.release_nameplate(name, "side4", 0)
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 3)

        # releasing one side leaves the second claim
        app.release_nameplate(name, "side1", 5)
        np_row, side_rows = self._nameplate(app, name)
        claims = [(row["side"], row["claimed"]) for row in side_rows]
        self.assertIn(("side1", False), claims)
        self.assertIn(("side2", True), claims)
        self.assertIn(("side3", True), claims)

        # releasing one side multiple times is ignored
        app.release_nameplate(name, "side1", 5)
        np_row, side_rows = self._nameplate(app, name)
        claims = [(row["side"], row["claimed"]) for row in side_rows]
        self.assertIn(("side1", False), claims)
        self.assertIn(("side2", True), claims)
        self.assertIn(("side3", True), claims)

        # release the second side
        app.release_nameplate(name, "side2", 6)
        np_row, side_rows = self._nameplate(app, name)
        claims = [(row["side"], row["claimed"]) for row in side_rows]
        self.assertIn(("side1", False), claims)
        self.assertIn(("side2", False), claims)
        self.assertIn(("side3", True), claims)

        # releasing the third side frees the nameplate, and adds usage
        app.release_nameplate(name, "side3", 7)
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(np_row, None)
        usage = app._db.execute("SELECT * FROM `nameplate_usage`").fetchone()
        self.assertEqual(usage["app_id"], "appid")
        self.assertEqual(usage["started"], 0)
        self.assertEqual(usage["waiting_time"], 3)
        self.assertEqual(usage["total_time"], 7)
        self.assertEqual(usage["result"], "crowded")


    def _mailbox(self, app, mailbox_id):
        mb_row = app._db.execute("SELECT * FROM `mailboxes`"
                                 " WHERE `app_id`='appid' AND `id`=?",
                                 (mailbox_id,)).fetchone()
        if not mb_row:
            return None, None
        side_rows = app._db.execute("SELECT * FROM `mailbox_sides`"
                                    " WHERE `mailbox_id`=?",
                                    (mailbox_id,)).fetchall()
        return mb_row, side_rows

    def test_mailbox(self):
        app = self._rendezvous.get_app("appid")
        mailbox_id = "mid"
        m1 = app.open_mailbox(mailbox_id, "side1", 0)

        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["side"], "side1")
        self.assertEqual(side_rows[0]["added"], 0)

        # opening the same mailbox twice, by the same side, gets the same
        # object, and does not update the "added" timestamp
        self.assertIdentical(m1, app.open_mailbox(mailbox_id, "side1", 1))
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["side"], "side1")
        self.assertEqual(side_rows[0]["added"], 0)

        # opening a second side gets the same object, and adds a new claim
        self.assertIdentical(m1, app.open_mailbox(mailbox_id, "side2", 2))
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(len(side_rows), 2)
        adds = [(row["side"], row["added"]) for row in side_rows]
        self.assertIn(("side1", 0), adds)
        self.assertIn(("side2", 2), adds)

        # a third open marks it as crowded
        self.assertRaises(rendezvous.CrowdedError,
                          app.open_mailbox, mailbox_id, "side3", 3)
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(len(side_rows), 3)
        m1.close("side3", "company", 4)

        # closing a side that never claimed the mailbox is ignored
        m1.close("side4", "mood", 4)
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(len(side_rows), 3)

        # closing one side leaves the second claim
        m1.close("side1", "mood", 5)
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        sides = [(row["side"], row["opened"], row["mood"]) for row in side_rows]
        self.assertIn(("side1", False, "mood"), sides)
        self.assertIn(("side2", True, None), sides)
        self.assertIn(("side3", False, "company"), sides)

        # closing one side multiple times is ignored
        m1.close("side1", "mood", 6)
        mb_row, side_rows = self._mailbox(app, mailbox_id)
        sides = [(row["side"], row["opened"], row["mood"]) for row in side_rows]
        self.assertIn(("side1", False, "mood"), sides)
        self.assertIn(("side2", True, None), sides)
        self.assertIn(("side3", False, "company"), sides)

        l1 = []; stop1 = []; stop1_f = lambda: stop1.append(True)
        m1.add_listener("handle1", l1.append, stop1_f)

        # closing the second side frees the mailbox, and adds usage
        m1.close("side2", "mood", 7)
        self.assertEqual(stop1, [True])

        mb_row, side_rows = self._mailbox(app, mailbox_id)
        self.assertEqual(mb_row, None)
        usage = app._db.execute("SELECT * FROM `mailbox_usage`").fetchone()
        self.assertEqual(usage["app_id"], "appid")
        self.assertEqual(usage["started"], 0)
        self.assertEqual(usage["waiting_time"], 2)
        self.assertEqual(usage["total_time"], 7)
        self.assertEqual(usage["result"], "crowded")

    def _messages(self, app):
        c = app._db.execute("SELECT * FROM `messages`"
                            " WHERE `app_id`='appid' AND `mailbox_id`='mid'")
        return c.fetchall()

    def test_messages(self):
        app = self._rendezvous.get_app("appid")
        mailbox_id = "mid"
        m1 = app.open_mailbox(mailbox_id, "side1", 0)
        m1.add_message(SidedMessage(side="side1", phase="phase",
                                    body="body", server_rx=1,
                                    msg_id="msgid"))
        msgs = self._messages(app)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["body"], "body")

        l1 = []; stop1 = []; stop1_f = lambda: stop1.append(True)
        l2 = []; stop2 = []; stop2_f = lambda: stop2.append(True)
        old = m1.add_listener("handle1", l1.append, stop1_f)
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0].side, "side1")
        self.assertEqual(old[0].body, "body")

        m1.add_message(SidedMessage(side="side1", phase="phase2",
                                    body="body2", server_rx=1,
                                    msg_id="msgid"))
        self.assertEqual(len(l1), 1)
        self.assertEqual(l1[0].body, "body2")
        old = m1.add_listener("handle2", l2.append, stop2_f)
        self.assertEqual(len(old), 2)

        m1.add_message(SidedMessage(side="side1", phase="phase3",
                                    body="body3", server_rx=1,
                                    msg_id="msgid"))
        self.assertEqual(len(l1), 2)
        self.assertEqual(l1[-1].body, "body3")
        self.assertEqual(len(l2), 1)
        self.assertEqual(l2[-1].body, "body3")

        m1.remove_listener("handle1")

        m1.add_message(SidedMessage(side="side1", phase="phase4",
                                    body="body4", server_rx=1,
                                    msg_id="msgid"))
        self.assertEqual(len(l1), 2)
        self.assertEqual(l1[-1].body, "body3")
        self.assertEqual(len(l2), 2)
        self.assertEqual(l2[-1].body, "body4")

        m1._shutdown()
        self.assertEqual(stop1, [])
        self.assertEqual(stop2, [True])

        # message adds are not idempotent: clients filter duplicates
        m1.add_message(SidedMessage(side="side1", phase="phase",
                                    body="body", server_rx=1,
                                    msg_id="msgid"))
        msgs = self._messages(app)
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[-1]["body"], "body")

class Prune(unittest.TestCase):

    def _get_mailbox_updated(self, app, mbox_id):
        row = app._db.execute("SELECT * FROM `mailboxes` WHERE"
                              " `app_id`=? AND `id`=?",
                              (app._app_id, mbox_id)).fetchone()
        return row["updated"]

    def test_update(self):
        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, None)
        app = rv.get_app("appid")
        mbox_id = "mbox1"
        app.open_mailbox(mbox_id, "side1", 1)
        self.assertEqual(self._get_mailbox_updated(app, mbox_id), 1)

        mb = app.open_mailbox(mbox_id, "side2", 2)
        self.assertEqual(self._get_mailbox_updated(app, mbox_id), 2)

        sm = SidedMessage("side1", "phase", "body", 3, "msgid")
        mb.add_message(sm)
        self.assertEqual(self._get_mailbox_updated(app, mbox_id), 3)

    def test_apps(self):
        rv = rendezvous.Rendezvous(get_db(":memory:"), None, None)
        app = rv.get_app("appid")
        app.allocate_nameplate("side", 121)
        app.prune = mock.Mock()
        rv.prune_all_apps(now=123, old=122)
        self.assertEqual(app.prune.mock_calls, [mock.call(123, 122)])

    def test_nameplates(self):
        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, 3600)

        # timestamps <=50 are "old", >=51 are "new"
        #OLD = "old"; NEW = "new"
        #when = {OLD: 1, NEW: 60}
        new_nameplates = set()

        APPID = "appid"
        app = rv.get_app(APPID)

        # Exercise the first-vs-second newness tests
        app.claim_nameplate("np-1", "side1", 1)
        app.claim_nameplate("np-2", "side1", 1)
        app.claim_nameplate("np-2", "side2", 2)
        app.claim_nameplate("np-3", "side1", 60)
        new_nameplates.add("np-3")
        app.claim_nameplate("np-4", "side1", 1)
        app.claim_nameplate("np-4", "side2", 60)
        new_nameplates.add("np-4")
        app.claim_nameplate("np-5", "side1", 60)
        app.claim_nameplate("np-5", "side2", 61)
        new_nameplates.add("np-5")

        rv.prune_all_apps(now=123, old=50)

        nameplates = set([row["name"] for row in
                          db.execute("SELECT * FROM `nameplates`").fetchall()])
        self.assertEqual(new_nameplates, nameplates)
        mailboxes = set([row["id"] for row in
                         db.execute("SELECT * FROM `mailboxes`").fetchall()])
        self.assertEqual(len(new_nameplates), len(mailboxes))

    def test_mailboxes(self):
        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, 3600)

        # timestamps <=50 are "old", >=51 are "new"
        #OLD = "old"; NEW = "new"
        #when = {OLD: 1, NEW: 60}
        new_mailboxes = set()

        APPID = "appid"
        app = rv.get_app(APPID)

        # Exercise the first-vs-second newness tests
        app.open_mailbox("mb-11", "side1", 1)
        app.open_mailbox("mb-12", "side1", 1)
        app.open_mailbox("mb-12", "side2", 2)
        app.open_mailbox("mb-13", "side1", 60)
        new_mailboxes.add("mb-13")
        app.open_mailbox("mb-14", "side1", 1)
        app.open_mailbox("mb-14", "side2", 60)
        new_mailboxes.add("mb-14")
        app.open_mailbox("mb-15", "side1", 60)
        app.open_mailbox("mb-15", "side2", 61)
        new_mailboxes.add("mb-15")

        rv.prune_all_apps(now=123, old=50)

        mailboxes = set([row["id"] for row in
                         db.execute("SELECT * FROM `mailboxes`").fetchall()])
        self.assertEqual(new_mailboxes, mailboxes)

    def test_lots(self):
        OLD = "old"; NEW = "new"
        for nameplate in [False, True]:
            for mailbox in [OLD, NEW]:
                for has_listeners in [False, True]:
                    self.one(nameplate, mailbox, has_listeners)

    def test_one(self):
       # to debug specific problems found by test_lots
       self.one(None, "new", False)

    def one(self, nameplate, mailbox, has_listeners):
        desc = ("nameplate=%s, mailbox=%s, has_listeners=%s" %
                (nameplate, mailbox, has_listeners))
        log.msg(desc)

        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, 3600)
        APPID = "appid"
        app = rv.get_app(APPID)

        # timestamps <=50 are "old", >=51 are "new"
        OLD = "old"; NEW = "new"
        when = {OLD: 1, NEW: 60}
        nameplate_survives = False
        mailbox_survives = False

        mbid = "mbid"
        if nameplate:
            mbid = app.claim_nameplate("npid", "side1", when[mailbox])
        mb = app.open_mailbox(mbid, "side1", when[mailbox])

        # the pruning algorithm doesn't care about the age of messages,
        # because mailbox.updated is always updated each time we add a
        # message
        sm = SidedMessage("side1", "phase", "body", when[mailbox], "msgid")
        mb.add_message(sm)

        if has_listeners:
            mb.add_listener("handle", None, None)

        if (mailbox == NEW or has_listeners):
            if nameplate:
                nameplate_survives = True
            mailbox_survives = True
        messages_survive = mailbox_survives

        rv.prune_all_apps(now=123, old=50)

        nameplates = set([row["name"] for row in
                          db.execute("SELECT * FROM `nameplates`").fetchall()])
        self.assertEqual(nameplate_survives, bool(nameplates),
                         ("nameplate", nameplate_survives, nameplates, desc))

        mailboxes = set([row["id"] for row in
                         db.execute("SELECT * FROM `mailboxes`").fetchall()])
        self.assertEqual(mailbox_survives, bool(mailboxes),
                         ("mailbox", mailbox_survives, mailboxes, desc))

        messages = set([row["msg_id"] for row in
                          db.execute("SELECT * FROM `messages`").fetchall()])
        self.assertEqual(messages_survive, bool(messages),
                         ("messages", messages_survive, messages, desc))


def strip_message(msg):
    m2 = msg.copy()
    m2.pop("id", None)
    m2.pop("server_rx", None)
    return m2

def strip_messages(messages):
    return [strip_message(m) for m in messages]

class WSClient(websocket.WebSocketClientProtocol):
    def __init__(self):
        websocket.WebSocketClientProtocol.__init__(self)
        self.events = []
        self.errors = []
        self.d = None
        self.ping_counter = itertools.count(0)
    def onOpen(self):
        self.factory.d.callback(self)
    def onMessage(self, payload, isBinary):
        assert not isBinary
        event = json.loads(payload.decode("utf-8"))
        if event["type"] == "error":
            self.errors.append(event)
        if self.d:
            assert not self.events
            d,self.d = self.d,None
            d.callback(event)
            return
        self.events.append(event)

    def close(self):
        self.d = defer.Deferred()
        self.transport.loseConnection()
        return self.d
    def onClose(self, wasClean, code, reason):
        if self.d:
            self.d.callback((wasClean, code, reason))

    def next_event(self):
        assert not self.d
        if self.events:
            event = self.events.pop(0)
            return defer.succeed(event)
        self.d = defer.Deferred()
        return self.d

    @inlineCallbacks
    def next_non_ack(self):
        while True:
            m = yield self.next_event()
            if m["type"] != "ack":
                returnValue(m)

    def strip_acks(self):
        self.events = [e for e in self.events if e["type"] != "ack"]

    def send(self, mtype, **kwargs):
        kwargs["type"] = mtype
        payload = json.dumps(kwargs).encode("utf-8")
        self.sendMessage(payload, False)

    def send_notype(self, **kwargs):
        payload = json.dumps(kwargs).encode("utf-8")
        self.sendMessage(payload, False)

    @inlineCallbacks
    def sync(self):
        ping = next(self.ping_counter)
        self.send("ping", ping=ping)
        # queue all messages until the pong, then put them back
        old_events = []
        while True:
            ev = yield self.next_event()
            if ev["type"] == "pong" and ev["pong"] == ping:
                self.events = old_events + self.events
                returnValue(None)
            old_events.append(ev)

class WSFactory(websocket.WebSocketClientFactory):
    protocol = WSClient

class WSClientSync(unittest.TestCase):
    # make sure my 'sync' method actually works

    @inlineCallbacks
    def test_sync(self):
        sent = []
        c = WSClient()
        def _send(mtype, **kwargs):
            sent.append( (mtype, kwargs) )
        c.send = _send
        def add(mtype, **kwargs):
            kwargs["type"] = mtype
            c.onMessage(json.dumps(kwargs).encode("utf-8"), False)
        # no queued messages
        sunc = []
        d = c.sync()
        d.addBoth(sunc.append)
        self.assertEqual(sent, [("ping", {"ping": 0})])
        self.assertEqual(sunc, [])
        add("pong", pong=0)
        yield d
        self.assertEqual(c.events, [])

        # one,two,ping,pong
        add("one")
        add("two", two=2)
        sunc = []
        d = c.sync()
        d.addBoth(sunc.append)
        add("pong", pong=1)
        yield d
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "one")
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "two")
        self.assertEqual(c.events, [])

        # one,ping,two,pong
        add("one")
        sunc = []
        d = c.sync()
        d.addBoth(sunc.append)
        add("two", two=2)
        add("pong", pong=2)
        yield d
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "one")
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "two")
        self.assertEqual(c.events, [])

        # ping,one,two,pong
        sunc = []
        d = c.sync()
        d.addBoth(sunc.append)
        add("one")
        add("two", two=2)
        add("pong", pong=3)
        yield d
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "one")
        m = yield c.next_non_ack()
        self.assertEqual(m["type"], "two")
        self.assertEqual(c.events, [])



class WebSocketAPI(ServerBase, unittest.TestCase):
    def setUp(self):
        self._clients = []
        return ServerBase.setUp(self)

    def tearDown(self):
        for c in self._clients:
            c.transport.loseConnection()
        return ServerBase.tearDown(self)

    @inlineCallbacks
    def make_client(self):
        f = WSFactory(self.relayurl)
        f.d = defer.Deferred()
        reactor.connectTCP("127.0.0.1", self.rdv_ws_port, f)
        c = yield f.d
        self._clients.append(c)
        returnValue(c)

    def check_welcome(self, data):
        self.failUnlessIn("welcome", data)
        self.failUnlessEqual(data["welcome"],
                             {"current_cli_version": __version__})

    @inlineCallbacks
    def test_welcome(self):
        c1 = yield self.make_client()
        msg = yield c1.next_non_ack()
        self.check_welcome(msg)
        self.assertEqual(self._rendezvous._apps, {})

    @inlineCallbacks
    def test_bind(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()

        c1.send("bind", appid="appid") # missing side=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "bind requires 'side'")

        c1.send("bind", side="side") # missing appid=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "bind requires 'appid'")

        c1.send("bind", appid="appid", side="side")
        yield c1.sync()
        self.assertEqual(list(self._rendezvous._apps.keys()), ["appid"])

        c1.send("bind", appid="appid", side="side") # duplicate
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "already bound")

        c1.send_notype(other="misc") # missing 'type'
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "missing 'type'")

        c1.send("___unknown") # unknown type
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "unknown type")

        c1.send("ping") # missing 'ping'
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "ping requires 'ping'")

    @inlineCallbacks
    def test_list(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()

        c1.send("list") # too early, must bind first
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "must bind first")

        c1.send("bind", appid="appid", side="side")
        c1.send("list")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "nameplates")
        self.assertEqual(m["nameplates"], [])

        app = self._rendezvous.get_app("appid")
        nameplate_id1 = app.allocate_nameplate("side", 0)
        app.claim_nameplate("np2", "side", 0)

        c1.send("list")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "nameplates")
        nids = set()
        for n in m["nameplates"]:
            self.assertEqual(type(n), dict)
            self.assertEqual(list(n.keys()), ["id"])
            nids.add(n["id"])
        self.assertEqual(nids, set([nameplate_id1, "np2"]))

    def _nameplate(self, app, name):
        np_row = app._db.execute("SELECT * FROM `nameplates`"
                                 " WHERE `app_id`='appid' AND `name`=?",
                                 (name,)).fetchone()
        if not np_row:
            return None, None
        npid = np_row["id"]
        side_rows = app._db.execute("SELECT * FROM `nameplate_sides`"
                                    " WHERE `nameplates_id`=?",
                                    (npid,)).fetchall()
        return np_row, side_rows

    @inlineCallbacks
    def test_allocate(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()

        c1.send("allocate") # too early, must bind first
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "must bind first")

        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")
        c1.send("allocate")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "allocated")
        name = m["nameplate"]

        nids = app.get_nameplate_ids()
        self.assertEqual(len(nids), 1)
        self.assertEqual(name, list(nids)[0])

        c1.send("allocate")
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"],
                         "you already allocated one, don't be greedy")

        c1.send("claim", nameplate=name) # allocate+claim is ok
        yield c1.sync()
        np_row, side_rows = self._nameplate(app, name)
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["side"], "side")

    @inlineCallbacks
    def test_claim(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        c1.send("claim") # missing nameplate=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "claim requires 'nameplate'")

        c1.send("claim", nameplate="np1")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "claimed")
        mailbox_id = m["mailbox"]
        self.assertEqual(type(mailbox_id), type(""))

        nids = app.get_nameplate_ids()
        self.assertEqual(len(nids), 1)
        self.assertEqual("np1", list(nids)[0])
        np_row, side_rows = self._nameplate(app, "np1")
        self.assertEqual(len(side_rows), 1)
        self.assertEqual(side_rows[0]["side"], "side")

        # claiming a nameplate assigns a random mailbox id and creates the
        # mailbox row
        mailboxes = app._db.execute("SELECT * FROM `mailboxes`"
                                    " WHERE `app_id`='appid'").fetchall()
        self.assertEqual(len(mailboxes), 1)

    @inlineCallbacks
    def test_claim_crowded(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        app.claim_nameplate("np1", "side1", 0)
        app.claim_nameplate("np1", "side2", 0)

        # the third claim will signal crowding
        c1.send("claim", nameplate="np1")
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "crowded")

    @inlineCallbacks
    def test_release(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        app.claim_nameplate("np1", "side2", 0)

        c1.send("release") # didn't do claim first
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"],
                         "must claim a nameplate before releasing it")

        c1.send("claim", nameplate="np1")
        yield c1.next_non_ack()

        c1.send("release")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "released")

        np_row, side_rows = self._nameplate(app, "np1")
        claims = [(row["side"], row["claimed"]) for row in side_rows]
        self.assertIn(("side", False), claims)
        self.assertIn(("side2", True), claims)

        c1.send("release") # no longer claimed
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"],
                         "must claim a nameplate before releasing it")

    @inlineCallbacks
    def test_open(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        c1.send("open") # missing mailbox=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "open requires 'mailbox'")

        mb1 = app.open_mailbox("mb1", "side2", 0)
        mb1.add_message(SidedMessage(side="side2", phase="phase",
                                     body="body", server_rx=0,
                                     msg_id="msgid"))

        c1.send("open", mailbox="mb1")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "message")
        self.assertEqual(m["body"], "body")
        self.assertTrue(mb1.has_listeners())

        mb1.add_message(SidedMessage(side="side2", phase="phase2",
                                     body="body2", server_rx=0,
                                     msg_id="msgid"))
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "message")
        self.assertEqual(m["body"], "body2")

        c1.send("open", mailbox="mb1")
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "you already have a mailbox open")

    @inlineCallbacks
    def test_add(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")
        mb1 = app.open_mailbox("mb1", "side2", 0)
        l1 = []; stop1 = []; stop1_f = lambda: stop1.append(True)
        mb1.add_listener("handle1", l1.append, stop1_f)

        c1.send("add") # didn't open first
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "must open mailbox before adding")

        c1.send("open", mailbox="mb1")

        c1.send("add", body="body") # missing phase=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "missing 'phase'")

        c1.send("add", phase="phase") # missing body=
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "missing 'body'")

        c1.send("add", phase="phase", body="body")
        m = yield c1.next_non_ack() # echoed back
        self.assertEqual(m["type"], "message")
        self.assertEqual(m["body"], "body")

        self.assertEqual(len(l1), 1)
        self.assertEqual(l1[0].body, "body")

    @inlineCallbacks
    def test_close(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        c1.send("close", mood="mood") # must open first
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "must open mailbox before closing")

        c1.send("open", mailbox="mb1")
        yield c1.sync()
        mb1 = app._mailboxes["mb1"]
        self.assertTrue(mb1.has_listeners())

        c1.send("close", mood="mood")
        m = yield c1.next_non_ack()
        self.assertEqual(m["type"], "closed")
        self.assertFalse(mb1.has_listeners())

        c1.send("close", mood="mood") # already closed
        err = yield c1.next_non_ack()
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"], "must open mailbox before closing")

    @inlineCallbacks
    def test_disconnect(self):
        c1 = yield self.make_client()
        yield c1.next_non_ack()
        c1.send("bind", appid="appid", side="side")
        app = self._rendezvous.get_app("appid")

        c1.send("open", mailbox="mb1")
        yield c1.sync()
        mb1 = app._mailboxes["mb1"]
        self.assertTrue(mb1.has_listeners())

        yield c1.close()
        # wait for the server to notice the socket has closed
        started = time.time()
        while mb1.has_listeners() and (time.time()-started < 5.0):
            d = defer.Deferred()
            reactor.callLater(0.01, d.callback, None)
            yield d
        self.assertFalse(mb1.has_listeners())


class Summary(unittest.TestCase):
    def test_mailbox(self):
        app = rendezvous.AppNamespace(None, None, False, None)
        # starts at time 1, maybe gets second open at time 3, closes at 5
        def s(rows, pruned=False):
            return app._summarize_mailbox(rows, 5, pruned)

        rows = [dict(added=1)]
        self.assertEqual(s(rows), Usage(1, None, 4, "lonely"))
        rows = [dict(added=1, mood="lonely")]
        self.assertEqual(s(rows), Usage(1, None, 4, "lonely"))
        rows = [dict(added=1, mood="errory")]
        self.assertEqual(s(rows), Usage(1, None, 4, "errory"))
        rows = [dict(added=1, mood=None)]
        self.assertEqual(s(rows, pruned=True), Usage(1, None, 4, "pruney"))
        rows = [dict(added=1, mood="happy")]
        self.assertEqual(s(rows, pruned=True), Usage(1, None, 4, "pruney"))

        rows = [dict(added=1, mood="happy"), dict(added=3, mood="happy")]
        self.assertEqual(s(rows), Usage(1, 2, 4, "happy"))

        rows = [dict(added=1, mood="errory"), dict(added=3, mood="happy")]
        self.assertEqual(s(rows), Usage(1, 2, 4, "errory"))

        rows = [dict(added=1, mood="happy"), dict(added=3, mood="errory")]
        self.assertEqual(s(rows), Usage(1, 2, 4, "errory"))

        rows = [dict(added=1, mood="scary"), dict(added=3, mood="happy")]
        self.assertEqual(s(rows), Usage(1, 2, 4, "scary"))

        rows = [dict(added=1, mood="scary"), dict(added=3, mood="errory")]
        self.assertEqual(s(rows), Usage(1, 2, 4, "scary"))

        rows = [dict(added=1, mood="happy"), dict(added=3, mood=None)]
        self.assertEqual(s(rows, pruned=True), Usage(1, 2, 4, "pruney"))
        rows = [dict(added=1, mood="happy"), dict(added=3, mood="happy")]
        self.assertEqual(s(rows, pruned=True), Usage(1, 2, 4, "pruney"))

        rows = [dict(added=1), dict(added=3), dict(added=4)]
        self.assertEqual(s(rows), Usage(1, 2, 4, "crowded"))

        rows = [dict(added=1), dict(added=3), dict(added=4)]
        self.assertEqual(s(rows, pruned=True), Usage(1, 2, 4, "crowded"))

    def test_nameplate(self):
        a = rendezvous.AppNamespace(None, None, False, None)
        # starts at time 1, maybe gets second open at time 3, closes at 5
        def s(rows, pruned=False):
            return a._summarize_nameplate_usage(rows, 5, pruned)

        rows = [dict(added=1)]
        self.assertEqual(s(rows), Usage(1, None, 4, "lonely"))
        rows = [dict(added=1), dict(added=3)]
        self.assertEqual(s(rows), Usage(1, 2, 4, "happy"))

        rows = [dict(added=1), dict(added=3)]
        self.assertEqual(s(rows, pruned=True), Usage(1, 2, 4, "pruney"))

        rows = [dict(added=1), dict(added=3), dict(added=4)]
        self.assertEqual(s(rows), Usage(1, 2, 4, "crowded"))


    def test_blur(self):
        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, 3600)
        APPID = "appid"
        app = rv.get_app(APPID)
        app.claim_nameplate("npid", "side1", 10) # start time is 10
        rv.prune_all_apps(now=123, old=50)
        # start time should be rounded to top of the hour (blur_usage=3600)
        row = db.execute("SELECT * FROM `nameplate_usage`").fetchone()
        self.assertEqual(row["started"], 0)

        app = rv.get_app(APPID)
        app.open_mailbox("mbid", "side1", 20) # start time is 20
        rv.prune_all_apps(now=123, old=50)
        row = db.execute("SELECT * FROM `mailbox_usage`").fetchone()
        self.assertEqual(row["started"], 0)

    def test_no_blur(self):
        db = get_db(":memory:")
        rv = rendezvous.Rendezvous(db, None, None)
        APPID = "appid"
        app = rv.get_app(APPID)
        app.claim_nameplate("npid", "side1", 10) # start time is 10
        rv.prune_all_apps(now=123, old=50)
        row = db.execute("SELECT * FROM `nameplate_usage`").fetchone()
        self.assertEqual(row["started"], 10)

        db.execute("DELETE FROM `mailbox_usage`")
        db.commit()
        app = rv.get_app(APPID)
        app.open_mailbox("mbid", "side1", 20) # start time is 20
        rv.prune_all_apps(now=123, old=50)
        row = db.execute("SELECT * FROM `mailbox_usage`").fetchone()
        self.assertEqual(row["started"], 20)


class Accumulator(protocol.Protocol):
    def __init__(self):
        self.data = b""
        self.count = 0
        self._wait = None
    def waitForBytes(self, more):
        assert self._wait is None
        self.count = more
        self._wait = defer.Deferred()
        self._check_done()
        return self._wait
    def dataReceived(self, data):
        self.data = self.data + data
        self._check_done()
    def _check_done(self):
        if self._wait and len(self.data) >= self.count:
            d = self._wait
            self._wait = None
            d.callback(self)
    def connectionLost(self, why):
        if self._wait:
            self._wait.errback(RuntimeError("closed"))

class Transit(ServerBase, unittest.TestCase):
    def test_blur_size(self):
        blur = transit_server.blur_size
        self.failUnlessEqual(blur(0), 0)
        self.failUnlessEqual(blur(1), 10e3)
        self.failUnlessEqual(blur(10e3), 10e3)
        self.failUnlessEqual(blur(10e3+1), 20e3)
        self.failUnlessEqual(blur(15e3), 20e3)
        self.failUnlessEqual(blur(20e3), 20e3)
        self.failUnlessEqual(blur(1e6), 1e6)
        self.failUnlessEqual(blur(1e6+1), 2e6)
        self.failUnlessEqual(blur(1.5e6), 2e6)
        self.failUnlessEqual(blur(2e6), 2e6)
        self.failUnlessEqual(blur(900e6), 900e6)
        self.failUnlessEqual(blur(1000e6), 1000e6)
        self.failUnlessEqual(blur(1050e6), 1100e6)
        self.failUnlessEqual(blur(1100e6), 1100e6)
        self.failUnlessEqual(blur(1150e6), 1200e6)

    @defer.inlineCallbacks
    def test_web_request(self):
        resp = yield client.getPage('http://127.0.0.1:{}/'.format(self.relayport).encode('ascii'))
        self.assertEqual('Wormhole Relay'.encode('ascii'), resp.strip())

    @defer.inlineCallbacks
    def test_basic(self):
        ep = clientFromString(reactor, self.transit)
        a1 = yield connectProtocol(ep, Accumulator())
        a2 = yield connectProtocol(ep, Accumulator())

        token1 = b"\x00"*32
        a1.transport.write(b"please relay " + hexlify(token1) + b"\n")
        a2.transport.write(b"please relay " + hexlify(token1) + b"\n")

        # a correct handshake yields an ack, after which we can send
        exp = b"ok\n"
        yield a1.waitForBytes(len(exp))
        self.assertEqual(a1.data, exp)
        s1 = b"data1"
        a1.transport.write(s1)

        exp = b"ok\n"
        yield a2.waitForBytes(len(exp))
        self.assertEqual(a2.data, exp)

        # all data they sent after the handshake should be given to us
        exp = b"ok\n"+s1
        yield a2.waitForBytes(len(exp))
        self.assertEqual(a2.data, exp)

        a1.transport.loseConnection()
        a2.transport.loseConnection()

    @defer.inlineCallbacks
    def test_bad_handshake(self):
        ep = clientFromString(reactor, self.transit)
        a1 = yield connectProtocol(ep, Accumulator())

        token1 = b"\x00"*32
        # the server waits for the exact number of bytes in the expected
        # handshake message. to trigger "bad handshake", we must match.
        a1.transport.write(b"please DELAY " + hexlify(token1) + b"\n")

        exp = b"bad handshake\n"
        yield a1.waitForBytes(len(exp))
        self.assertEqual(a1.data, exp)

        a1.transport.loseConnection()

    @defer.inlineCallbacks
    def test_impatience(self):
        ep = clientFromString(reactor, self.transit)
        a1 = yield connectProtocol(ep, Accumulator())

        token1 = b"\x00"*32
        # sending too many bytes is impatience.
        a1.transport.write(b"please RELAY NOWNOW " + hexlify(token1) + b"\n")

        exp = b"impatient\n"
        yield a1.waitForBytes(len(exp))
        self.assertEqual(a1.data, exp)

        a1.transport.loseConnection()

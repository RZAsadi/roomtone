"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "app.py"), "utf8");
const clientMatch = source.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(clientMatch, "embedded browser client script must exist");
const client = clientMatch[1];

function extract(start, end) {
  const from = client.indexOf(start);
  const to = client.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `could not extract ${start}`);
  return client.slice(from, to).trim();
}

const negotiationSource = extract("function requestNegotiation(", "function createPeer(");
const messageHandlerSource = extract("async function handleMessage(m)", "function toggleMute(");

function makeHarness(peer) {
  const sent = [];
  const events = [];
  const context = {
    state: { id: 1, peers: new Map([[peer.id, peer]]) },
    send: message => { sent.push(message); events.push(`send:${message.type}`); },
    say: () => {},
    createPeer: () => peer,
    flushCandidates: async () => {},
    drawPeople: () => {},
    refreshRooms: async () => {},
    cleanup: () => {},
  };
  vm.createContext(context);
  vm.runInContext(`${negotiationSource}\n${messageHandlerSource}`, context);
  return { context, sent, events };
}

test("renegotiation waits for stable signaling", async () => {
  let offerCount = 0;
  const pc = {
    signalingState: "have-local-offer",
    localDescription: null,
    createOffer: async () => { offerCount += 1; return { type: "offer", sdp: "offer" }; },
    setLocalDescription: async description => {
      pc.localDescription = description;
      pc.signalingState = "have-local-offer";
    },
  };
  const peer = {
    id: 2, name: "Guest", pc, makingOffer: false, negotiationRunning: false,
    pendingNegotiation: false, pendingIceRestart: false, negotiationTask: null,
    remoteOfferPending: false,
  };
  const harness = makeHarness(peer);

  harness.context.requestNegotiation(peer);
  assert.equal(offerCount, 0);
  assert.equal(peer.pendingNegotiation, true);

  pc.signalingState = "stable";
  await harness.context.requestNegotiation(peer);
  assert.equal(offerCount, 1);
  assert.equal(harness.sent.filter(message => message.type === "offer").length, 1);
});

test("a failed offer keeps the negotiation request pending for a later retry", async () => {
  let offerCount = 0;
  const pc = {
    signalingState: "stable",
    localDescription: null,
    createOffer: async () => {
      offerCount += 1;
      if (offerCount === 1) throw new Error("temporary offer failure");
      return { type: "offer", sdp: "retry-offer" };
    },
    setLocalDescription: async description => {
      pc.localDescription = description;
      pc.signalingState = "have-local-offer";
    },
  };
  const peer = {
    id: 2, name: "Guest", pc, makingOffer: false, negotiationRunning: false,
    pendingNegotiation: false, pendingIceRestart: false, negotiationTask: null,
    remoteOfferPending: false,
  };
  const harness = makeHarness(peer);

  await harness.context.requestNegotiation(peer);
  assert.equal(peer.pendingNegotiation, true);
  assert.equal(offerCount, 1);

  await harness.context.requestNegotiation(peer);
  assert.equal(peer.pendingNegotiation, false);
  assert.equal(offerCount, 2);
  assert.equal(harness.sent.filter(message => message.type === "offer").length, 1);
});

test("a colliding remote offer waits for an in-flight local offer and drains queued role changes afterward", async () => {
  let releaseFirstOffer;
  let createOfferCount = 0;
  const events = [];
  const pc = {
    signalingState: "stable",
    localDescription: null,
    remoteDescription: null,
    restartIce: () => events.push("restartIce"),
    createOffer: () => {
      createOfferCount += 1;
      events.push(`createOffer:${createOfferCount}`);
      if (createOfferCount === 1) return new Promise(resolve => { releaseFirstOffer = resolve; });
      return Promise.resolve({ type: "offer", sdp: `local-${createOfferCount}` });
    },
    setLocalDescription: async description => {
      events.push(`setLocal:${description.type}`);
      if (description.type === "rollback") {
        pc.signalingState = "stable";
        pc.localDescription = null;
      } else {
        pc.localDescription = description;
        pc.signalingState = description.type === "offer" ? "have-local-offer" : "stable";
      }
    },
    setRemoteDescription: async description => {
      events.push(`setRemote:${description.type}`);
      pc.remoteDescription = description;
      pc.signalingState = "have-remote-offer";
    },
    createAnswer: async () => {
      events.push("createAnswer");
      return { type: "answer", sdp: "answer" };
    },
  };
  const peer = {
    id: 2, name: "Guest", pc, polite: true, makingOffer: false, ignoreOffer: false,
    negotiationRunning: false, pendingNegotiation: false, pendingIceRestart: false,
    negotiationTask: null, remoteOfferPending: false, pendingCandidates: [],
  };
  const harness = makeHarness(peer);
  const originalSend = harness.context.send;
  harness.context.send = message => {
    events.push(`send:${message.type}`);
    originalSend(message);
  };

  const localTask = harness.context.requestNegotiation(peer);
  harness.context.requestNegotiation(peer); // A role change arrives while createOffer is pending.
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof releaseFirstOffer, "function");

  const remoteTask = harness.context.handleMessage({
    type: "offer", from: peer.id, name: peer.name, sdp: { type: "offer", sdp: "remote" },
  });
  assert.equal(peer.remoteOfferPending, true);
  releaseFirstOffer({ type: "offer", sdp: "local-1" });
  await localTask;
  await remoteTask;
  if (peer.negotiationTask) await peer.negotiationTask;

  assert.equal(events.filter(event => event === "send:answer").length, 1);
  assert.equal(events.filter(event => event === "send:offer").length, 2);
  const answerIndex = events.indexOf("send:answer");
  const secondOfferIndex = events.lastIndexOf("send:offer");
  assert.ok(answerIndex >= 0 && secondOfferIndex > answerIndex, events.join(", "));
  assert.equal(peer.remoteOfferPending, false);
});

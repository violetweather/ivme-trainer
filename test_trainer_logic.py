#!/usr/bin/env python3
"""Offline logic tests for ivme_trainer (no running game needed).

Run:  python test_trainer_logic.py
Covers the movement-data signature, player-instance detection, the
noclip collision toggle, fly velocity writes, and intensity scaling,
all against a fake in-memory backend.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ivme_trainer import (  # noqa: E402
    BODY_VELOCITY_OFFSET, INSTANCE_MEMBERS_OFFSET, NODE_PARENT_OFFSET,
    NOCLIP_SHAPE_OFFSET_Y, OBJECT_PTR_OFFSET,
    OBJECT_SCRIPT_INSTANCE_OFFSET, PLAYER_MEMBER_COUNT, PLAYER_TRAIL_TYPES,
    SLOT_ORIGINAL_COL_POS_Y, STATE_TYPES, VALUE_OFFSET, VARIANT_SIZE,
    MovementData, Trainer, clamp_intensity, match_type_trail,
    scaled_preset_value)

HERE = os.path.dirname(os.path.abspath(__file__))
data = MovementData(os.path.join(HERE, "movement_data.json"))
assert len(data.fields) == 54
assert len(PLAYER_TRAIL_TYPES) == PLAYER_MEMBER_COUNT - 54
assert len(STATE_TYPES) == 21


# --- fake memory backend -----------------------------------------------------

class FakeMemory:
    def __init__(self):
        self.regions = {}
        self._next = 0x200_0000_0000

    def alloc(self, size):
        addr = self._next
        self.regions[addr] = bytearray(size)
        self._next += (size + 0xFFF) & ~0xFFF
        return addr

    def _locate(self, addr, size):
        for base, buf in self.regions.items():
            if base <= addr and addr + size <= base + len(buf):
                return buf, addr - base
        return None, 0

    def read(self, addr, size):
        buf, off = self._locate(addr, size)
        return bytes(buf[off:off + size]) if buf is not None else None

    def write(self, addr, payload):
        buf, off = self._locate(addr, len(payload))
        if buf is None:
            return False
        buf[off:off + len(payload)] = payload
        return True

    def read_u64(self, addr):
        raw = self.read(addr, 8)
        return struct.unpack("<Q", raw)[0] if raw else 0

    def read_byte(self, addr):
        raw = self.read(addr, 1)
        return raw[0] if raw else None

    def write_byte(self, addr, value):
        return self.write(addr, bytes([value & 0xFF]))

    @staticmethod
    def heapish(ptr):
        return 0x10000000000 < ptr < 0x40000000000 and ptr % 8 == 0

    def is_alive(self):
        return True

    def put_variant(self, array_addr, slot, vtype, payload=b""):
        off = array_addr + slot * VARIANT_SIZE
        self.write(off, struct.pack("<I", vtype))
        if payload:
            self.write(off + VALUE_OFFSET, payload)

    def put_qword(self, addr, value):
        self.write(addr, struct.pack("<Q", value))


def build_player(mem: FakeMemory, is_replay=False):
    """Build a fake player script-instance + col_shape + movement state."""
    seg = mem.alloc(PLAYER_MEMBER_COUNT * VARIANT_SIZE)
    for i, f in enumerate(data.fields):
        if f["type"] == "f":
            mem.put_variant(seg, i, 3, struct.pack("<d", float(f["default"])))
        elif f["type"] == "v2":
            mem.put_variant(seg, i, 5, struct.pack("<2f", *f["default"]))
        else:
            mem.put_variant(seg, i, 9, struct.pack("<3f", *f["default"]))
    for i, t in enumerate(PLAYER_TRAIL_TYPES):
        mem.put_variant(seg, 54 + i, t)
    # runtime flags + a distinctive stock shape offset to prove capture
    mem.write_byte(seg + 57 * VARIANT_SIZE + VALUE_OFFSET, 1)   # can_move
    mem.write_byte(seg + 76 * VARIANT_SIZE + VALUE_OFFSET, 1 if is_replay else 0)
    mem.write(seg + SLOT_ORIGINAL_COL_POS_Y * VARIANT_SIZE + VALUE_OFFSET,
              struct.pack("<d", 0.25))
    # collision shape object: vtable in exe range, parent -> body object
    body = mem.alloc(0x800)
    mem.put_qword(body, 0x7FF700005678)
    mem.write(body + BODY_VELOCITY_OFFSET, struct.pack("<3f", 1.0, 2.0, 3.0))
    col = mem.alloc(0x800)
    mem.put_qword(col, 0x7FF700001234)
    mem.put_qword(col + NODE_PARENT_OFFSET, body)
    # movement state: obj -> (+0x68) instance -> (+0x50) members
    members = mem.alloc(len(STATE_TYPES) * VARIANT_SIZE)
    for i, t in enumerate(STATE_TYPES):
        mem.put_variant(members, i, t)
    mem.write(members + VALUE_OFFSET, struct.pack("<3f", 1.0, 2.0, 3.0))
    inst = mem.alloc(0x100)
    mem.put_qword(inst + INSTANCE_MEMBERS_OFFSET, members)
    state_obj = mem.alloc(0x200)
    mem.put_qword(state_obj + OBJECT_SCRIPT_INSTANCE_OFFSET, inst)
    # object slots: ObjectID at +8 (ignored), Object* at +16
    mem.put_qword(seg + 84 * VARIANT_SIZE + OBJECT_PTR_OFFSET, col)
    mem.put_qword(seg + 87 * VARIANT_SIZE + OBJECT_PTR_OFFSET, state_obj)
    return seg, col, members


# --- movement signature --------------------------------------------------------

def test_movement_signature():
    blob = bytearray(len(data.fields) * VARIANT_SIZE)
    for i, f in enumerate(data.fields):
        off = i * VARIANT_SIZE
        if f["type"] == "f":
            struct.pack_into("<I", blob, off, 3)
            struct.pack_into("<d", blob, off + VALUE_OFFSET, float(f["default"]))
        elif f["type"] == "v2":
            struct.pack_into("<I", blob, off, 5)
            struct.pack_into("<2f", blob, off + VALUE_OFFSET, *f["default"])
        else:
            struct.pack_into("<I", blob, off, 9)
            struct.pack_into("<3f", blob, off + VALUE_OFFSET, *f["default"])
    assert data.matches(bytes(blob), 0)
    assert not data.matches(b"\x00" * len(blob), 0)


# --- player detection, noclip, fly velocity -----------------------------------

def test_player_chain():
    mem = FakeMemory()
    seg, col, members = build_player(mem)
    ghost_seg, ghost_col, _ = build_player(mem, is_replay=True)
    trainer = Trainer(mem, data)
    trainer.arrays = [ghost_seg, seg]

    players = trainer.find_players()
    assert len(players) == 2, players
    local = players[0]
    assert local.segment == seg and not local.is_replay
    assert local.col_shape == col and local.state_members == members
    assert trainer.read_velocity(local) == (1.0, 2.0, 3.0)

    # fly velocity write only touches Y
    assert trainer.write_velocity_y(local, -8.0)
    assert trainer.read_velocity(local) == (1.0, -8.0, 3.0)
    # ...and the native CharacterBody3D velocity, so the tick doesn't undo it
    assert local.body and local.native_velocity_addr
    native = struct.unpack("<3f", mem.read(local.native_velocity_addr, 12))
    assert native == (1.0, -8.0, 3.0), native

    # noclip displaces the local player's shape (slot 55), not the ghost's,
    # and restoring writes back the captured stock offset (0.25, not 0.0)
    def col_pos(seg_addr):
        raw = mem.read(seg_addr + SLOT_ORIGINAL_COL_POS_Y * VARIANT_SIZE + VALUE_OFFSET, 8)
        return struct.unpack("<d", raw)[0]

    assert trainer.set_shape_displaced(players, displaced=True)
    assert col_pos(seg) == NOCLIP_SHAPE_OFFSET_Y
    assert col_pos(ghost_seg) == 0.25
    assert trainer.set_shape_displaced(players, displaced=False)
    assert col_pos(seg) == 0.25
    # a fresh trainer (no captured originals) restores the displaced value
    assert trainer.set_shape_displaced(players, displaced=True)
    fresh = Trainer(mem, data)
    fresh.arrays = [seg]
    assert fresh.set_shape_displaced(fresh.find_players(), displaced=False)
    assert col_pos(seg) == 0.0


def test_stale_shape_refused():
    mem = FakeMemory()
    seg, col, _ = build_player(mem)
    trainer = Trainer(mem, data)
    trainer.arrays = [seg]
    players = trainer.find_players()
    assert len(players) == 1
    mem.put_qword(col, 0xDEADBEEF)  # shape freed/reused: vtable changed
    assert not trainer.set_shape_displaced(players, displaced=True)
    raw = mem.read(seg + SLOT_ORIGINAL_COL_POS_Y * VARIANT_SIZE + VALUE_OFFSET, 8)
    assert struct.unpack("<d", raw)[0] == 0.25  # untouched


def test_select_primary_picks_simulating_instance():
    class AdvancingMemory(FakeMemory):
        advance_addr = None

        def read(self, addr, size):
            if addr == self.advance_addr and size == 8:
                buf, off = self._locate(addr, size)
                cur, = struct.unpack_from("<d", buf, off)
                struct.pack_into("<d", buf, off, cur + 0.05)
            return super().read(addr, size)

    mem = AdvancingMemory()
    frozen_seg, _, _ = build_player(mem)
    live_seg, _, live_members = build_player(mem)
    mem.advance_addr = live_members + VARIANT_SIZE + VALUE_OFFSET
    trainer = Trainer(mem, data)
    trainer.arrays = [frozen_seg, live_seg]
    players = trainer.find_players()
    assert len(players) == 2
    chosen = trainer.select_primary(players)
    assert chosen is not None and chosen.segment == live_seg


# --- helpers --------------------------------------------------------------------

def test_helpers():
    assert match_type_trail([24, 0, 3], (24, 24, 3), nil_ok_indices=(1,))
    assert not match_type_trail([24, 0, 3], (24, 24, 3))
    assert not match_type_trail([24], (24, 3))
    assert scaled_preset_value(5.0, 7.5, 1.0) == 7.5
    assert scaled_preset_value(5.0, 7.5, 2.0) == 10.0
    assert scaled_preset_value(5.0, 7.5, 0.0) == 5.0
    assert scaled_preset_value([1.0, 2.0], [2.0, 4.0], 0.5) == [1.5, 3.0]
    assert clamp_intensity(99) == 4.0
    assert clamp_intensity(0.01) == 0.25
    assert clamp_intensity(1.5) == 1.5
    # trainer float comparison tolerates float32 vector round-trips
    assert Trainer._close([0.11999999731779099], [0.12])
    assert not Trainer._close(None, 1.0)
    for preset in data.presets.values():
        for name in preset["values"]:
            assert name in data.index and name not in Trainer.RUNTIME_OWNED


if __name__ == "__main__":
    test_movement_signature()
    test_player_chain()
    test_stale_shape_refused()
    test_select_primary_picks_simulating_instance()
    test_helpers()
    print("ALL TRAINER LOGIC TESTS PASSED")

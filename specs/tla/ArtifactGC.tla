----------------------------- MODULE ArtifactGC -----------------------------
(***************************************************************************)
(* Normal (buffered) artifact operations vs. the GC mark-sweep.            *)
(*                                                                         *)
(* Companion to UploadGC.tla (which covers the direct-to-GCS large-upload  *)
(* protocol). This module models the cas/ namespace and every normal file  *)
(* operation that touches blob references:                                 *)
(*                                                                         *)
(*   - upsert_artifact (artifacts.py): storage.put to the content-        *)
(*     addressed key cas/{drive}/{sha256} FIRST, then the DB tx (head      *)
(*     update + new version row + same-tx retention prune), then the       *)
(*     post-tx immediate orphan delete of the previous head blob (global   *)
(*     reference check, skip-guard for blobs the prune just unreferenced). *)
(*     Content addressing means re-uploading previously-seen bytes         *)
(*     RE-PUTS AN EXISTING KEY, resetting its GCS generation/time_created. *)
(*   - copy_artifact: one tx, source row FOR UPDATE, dest head+version    *)
(*     point at the SAME blob (CAS sharing). Modeled atomic.               *)
(*   - purge (GC purge phase / deletion-design): hard row delete cascades  *)
(*     the version rows. Modeled atomic.                                   *)
(*   - prune_old_versions: same-tx removal of old version refs.            *)
(*   - GC mark-sweep (gc.py): live_set + cutoff frozen at mark; then, per  *)
(*     blob, the DECISION uses the listing's metadata snapshot while the   *)
(*     DELETE is by name and UNCONDITIONAL (storage.delete carries no      *)
(*     if_generation_match).                                               *)
(*                                                                         *)
(* Not modeled, and why that is safe for this invariant:                   *)
(*   - rename / metadata patch / soft delete / restore: none of them       *)
(*     change blob references (the mark query does not filter deleted_at). *)
(*   - reads/downloads: NoDangling is about referenced blobs existing,     *)
(*     not about in-flight readers (the §6 edge-table property).           *)
(*   - per-artifact write serialization is the DB row lock: each op's tx   *)
(*     is one atomic step here, faithfully.                                *)
(*                                                                         *)
(* Time: one tick ~ MARK_SWEEP_AGE (24h); ticks are boundary crossings,    *)
(* not durations. Any in-flight multi-step operation (an upsert, a GC      *)
(* round) is seconds-to-minutes long, so it may straddle at most ONE       *)
(* boundary instant: the Tick action blocks while an operation that        *)
(* started before the previous tick is still in flight.                    *)
(*                                                                         *)
(* SweepPinsGeneration = FALSE models storage.delete as shipped (delete    *)
(* by name). TRUE models the candidate fix: the sweep deletes with         *)
(* if_generation_match pinned to the listed generation, so a blob          *)
(* re-put after the listing survives (precondition fails -> skip).         *)
(*                                                                         *)
(* Invariant NoDangling: every blob referenced by an artifact head or a    *)
(* version row exists.                                                     *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
  Writers,             \* model values: concurrent writer processes
  Arts,                \* model values: artifact slots in one drive
  Hashes,              \* model values: content hashes (cas/ keys)
  NoHash,              \* model value: "no blob"
  MaxOps,              \* ops per writer (bound)
  MinAge,              \* MARK_SWEEP_AGE in ticks
  MaxClock,            \* clock bound
  SweepPinsGeneration, \* TRUE = sweep deletes are generation-pinned
  ImmediateOrphanDelete \* TRUE = the post-tx immediate orphan delete
                        \* (artifacts.py:1261-1277) runs; FALSE = removed
                        \* (orphans wait for the mark-sweep)

(* --algorithm artifactgc

variables
  now    = 0,
  blobE  = [h \in Hashes |-> FALSE],   \* cas/{drive}/{h} exists
  born   = [h \in Hashes |-> 0],       \* its listed generation/time_created;
                                       \* a re-put resets it (new generation)
  head   = [a \in Arts |-> NoHash],    \* artifacts.gcs_object
  vrefs  = [a \in Arts |-> {}],        \* artifact_versions.gcs_object per art
  \* GC round state: live_set + cutoff frozen at mark (gc.py:898,937);
  \* cand/seenBorn are the listing's per-blob decision + metadata snapshot
  live   = {},
  markT  = 0,
  cand   = [h \in Hashes |-> FALSE],
  seenBorn = [h \in Hashes |-> 0],
  \* op-duration bookkeeping for the Tick guard
  busy   = [w \in Writers |-> FALSE],
  ws     = [w \in Writers |-> 0],
  gbusy  = FALSE,
  gs     = 0;

define
  AllRefs == UNION {({head[a]} \ {NoHash}) \cup vrefs[a] : a \in Arts}
  NoDangling ==
    \A a \in Arts:
      /\ (head[a] = NoHash \/ blobE[head[a]])
      /\ \A h \in vrefs[a] : blobE[h]
end define;

\* ------------------------------- writers ------------------------------------
process w \in Writers
variables ops = 0, tgt = NoHash, newh = NoHash, prev = NoHash, prunedNow = {};
begin
WLoop:
  while ops < MaxOps do
    ops := ops + 1;
    either
      \* ---- buffered upsert (create / overwrite / CAS re-upload) ----
      with a \in Arts, h \in Hashes do
        tgt := a;
        newh := h;
      end with;
      busy[self] := TRUE;
      ws[self] := now;
WPut:     \* storage.put BEFORE the DB tx (artifacts.py:780). Re-puts an
          \* existing key too: new generation, time_created resets.
      blobE[newh] := TRUE;
      born[newh] := now;
WTx:      \* the commit tx: head swap + new version row + same-tx prune.
          \* prunedNow \subseteq old version refs models any versions_max
          \* (prune_old_versions runs inside this same tx).
      prev := head[tgt];
      with pr \in SUBSET (vrefs[tgt] \ {newh}) do
        prunedNow := pr;
        vrefs[tgt] := (vrefs[tgt] \ pr) \cup {newh};
      end with;
      head[tgt] := newh;
WClean:   \* post-tx immediate orphan delete (artifacts.py:1261-1277):
          \* global reference check now, unconditional delete in a later
          \* step (the TOCTOU shape as shipped). Skip-guard: a blob the
          \* prune just unreferenced waits for the mark-sweep (review F2).
      if /\ ImmediateOrphanDelete
         /\ prev /= NoHash
         /\ prev /= newh
         /\ prev \notin AllRefs
         /\ prev \notin prunedNow
      then
WDel:
        blobE[prev] := FALSE;
      end if;
WEnd:
      busy[self] := FALSE;
    or
      \* ---- copy_artifact: one tx, source row FOR UPDATE -> atomic ----
      with s \in Arts, d \in Arts do
        if head[s] /= NoHash then
          vrefs[d] := vrefs[d] \cup {head[s]};
          head[d] := head[s];
        end if;
      end with;
    or
      \* ---- purge: hard row delete cascades version rows (atomic tx).
      \* Soft delete / restore are not modeled: they keep the rows, so
      \* the mark query still sees every reference.
      with a \in Arts do
        vrefs[a] := {};
        head[a] := NoHash;
      end with;
    end either;
  end while;
end process;

\* ------------------------------- GC sweeper ---------------------------------
process gc = "GC"
begin
GRound:
  while TRUE do
    gbusy := TRUE;
    gs := now;
GMark:    \* live_set := heads UNION version refs; cutoff frozen (gc.py:898)
    live := AllRefs;
    markT := now;
GList:    \* list_blobs: per-blob verdict from the frozen live_set/cutoff
          \* and the LISTED metadata (generation snapshot).
    cand := [h \in Hashes |->
               /\ blobE[h]
               /\ h \notin live
               /\ born[h] + MinAge < markT];
    seenBorn := born;
GDel:     \* storage.delete per orphan. As shipped: by name, unconditional.
          \* Pinned variant: if_generation_match(listed generation) -- a
          \* blob re-put since the listing fails the precondition, skip.
    blobE := [h \in Hashes |->
                IF /\ cand[h]
                   /\ (~SweepPinsGeneration \/ born[h] = seenBorn[h])
                THEN FALSE
                ELSE blobE[h]];
    gbusy := FALSE;
  end while;
end process;

\* --------------------------------- clock ------------------------------------
\* A tick is a 24h-scale boundary crossing. Fast operations (an upsert's
\* put->tx->clean window, one GC round) straddle at most one: Tick blocks
\* while any operation that already absorbed a tick is still in flight.
process clk = "CLK"
begin
Tick:
  while now < MaxClock do
    await /\ \A p \in Writers : ~busy[p] \/ now <= ws[p]
          /\ (~gbusy \/ now <= gs);
    now := now + 1;
  end while;
end process;

end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "74419caf" /\ chksum(tla) = "aabea61c")
VARIABLES now, blobE, born, head, vrefs, live, markT, cand, seenBorn, busy, 
          ws, gbusy, gs, pc

(* define statement *)
AllRefs == UNION {({head[a]} \ {NoHash}) \cup vrefs[a] : a \in Arts}
NoDangling ==
  \A a \in Arts:
    /\ (head[a] = NoHash \/ blobE[head[a]])
    /\ \A h \in vrefs[a] : blobE[h]

VARIABLES ops, tgt, newh, prev, prunedNow

vars == << now, blobE, born, head, vrefs, live, markT, cand, seenBorn, busy, 
           ws, gbusy, gs, pc, ops, tgt, newh, prev, prunedNow >>

ProcSet == (Writers) \cup {"GC"} \cup {"CLK"}

Init == (* Global variables *)
        /\ now = 0
        /\ blobE = [h \in Hashes |-> FALSE]
        /\ born = [h \in Hashes |-> 0]
        /\ head = [a \in Arts |-> NoHash]
        /\ vrefs = [a \in Arts |-> {}]
        /\ live = {}
        /\ markT = 0
        /\ cand = [h \in Hashes |-> FALSE]
        /\ seenBorn = [h \in Hashes |-> 0]
        /\ busy = [w \in Writers |-> FALSE]
        /\ ws = [w \in Writers |-> 0]
        /\ gbusy = FALSE
        /\ gs = 0
        (* Process w *)
        /\ ops = [self \in Writers |-> 0]
        /\ tgt = [self \in Writers |-> NoHash]
        /\ newh = [self \in Writers |-> NoHash]
        /\ prev = [self \in Writers |-> NoHash]
        /\ prunedNow = [self \in Writers |-> {}]
        /\ pc = [self \in ProcSet |-> CASE self \in Writers -> "WLoop"
                                        [] self = "GC" -> "GRound"
                                        [] self = "CLK" -> "Tick"]

WLoop(self) == /\ pc[self] = "WLoop"
               /\ IF ops[self] < MaxOps
                     THEN /\ ops' = [ops EXCEPT ![self] = ops[self] + 1]
                          /\ \/ /\ \E a \in Arts:
                                     \E h \in Hashes:
                                       /\ tgt' = [tgt EXCEPT ![self] = a]
                                       /\ newh' = [newh EXCEPT ![self] = h]
                                /\ busy' = [busy EXCEPT ![self] = TRUE]
                                /\ ws' = [ws EXCEPT ![self] = now]
                                /\ pc' = [pc EXCEPT ![self] = "WPut"]
                                /\ UNCHANGED <<head, vrefs>>
                             \/ /\ \E s \in Arts:
                                     \E d \in Arts:
                                       IF head[s] /= NoHash
                                          THEN /\ vrefs' = [vrefs EXCEPT ![d] = vrefs[d] \cup {head[s]}]
                                               /\ head' = [head EXCEPT ![d] = head[s]]
                                          ELSE /\ TRUE
                                               /\ UNCHANGED << head, vrefs >>
                                /\ pc' = [pc EXCEPT ![self] = "WLoop"]
                                /\ UNCHANGED <<busy, ws, tgt, newh>>
                             \/ /\ \E a \in Arts:
                                     /\ vrefs' = [vrefs EXCEPT ![a] = {}]
                                     /\ head' = [head EXCEPT ![a] = NoHash]
                                /\ pc' = [pc EXCEPT ![self] = "WLoop"]
                                /\ UNCHANGED <<busy, ws, tgt, newh>>
                     ELSE /\ pc' = [pc EXCEPT ![self] = "Done"]
                          /\ UNCHANGED << head, vrefs, busy, ws, ops, tgt, 
                                          newh >>
               /\ UNCHANGED << now, blobE, born, live, markT, cand, seenBorn, 
                               gbusy, gs, prev, prunedNow >>

WPut(self) == /\ pc[self] = "WPut"
              /\ blobE' = [blobE EXCEPT ![newh[self]] = TRUE]
              /\ born' = [born EXCEPT ![newh[self]] = now]
              /\ pc' = [pc EXCEPT ![self] = "WTx"]
              /\ UNCHANGED << now, head, vrefs, live, markT, cand, seenBorn, 
                              busy, ws, gbusy, gs, ops, tgt, newh, prev, 
                              prunedNow >>

WTx(self) == /\ pc[self] = "WTx"
             /\ prev' = [prev EXCEPT ![self] = head[tgt[self]]]
             /\ \E pr \in SUBSET (vrefs[tgt[self]] \ {newh[self]}):
                  /\ prunedNow' = [prunedNow EXCEPT ![self] = pr]
                  /\ vrefs' = [vrefs EXCEPT ![tgt[self]] = (vrefs[tgt[self]] \ pr) \cup {newh[self]}]
             /\ head' = [head EXCEPT ![tgt[self]] = newh[self]]
             /\ pc' = [pc EXCEPT ![self] = "WClean"]
             /\ UNCHANGED << now, blobE, born, live, markT, cand, seenBorn, 
                             busy, ws, gbusy, gs, ops, tgt, newh >>

WClean(self) == /\ pc[self] = "WClean"
                /\ IF /\ ImmediateOrphanDelete
                      /\ prev[self] /= NoHash
                      /\ prev[self] /= newh[self]
                      /\ prev[self] \notin AllRefs
                      /\ prev[self] \notin prunedNow[self]
                      THEN /\ pc' = [pc EXCEPT ![self] = "WDel"]
                      ELSE /\ pc' = [pc EXCEPT ![self] = "WEnd"]
                /\ UNCHANGED << now, blobE, born, head, vrefs, live, markT, 
                                cand, seenBorn, busy, ws, gbusy, gs, ops, tgt, 
                                newh, prev, prunedNow >>

WDel(self) == /\ pc[self] = "WDel"
              /\ blobE' = [blobE EXCEPT ![prev[self]] = FALSE]
              /\ pc' = [pc EXCEPT ![self] = "WEnd"]
              /\ UNCHANGED << now, born, head, vrefs, live, markT, cand, 
                              seenBorn, busy, ws, gbusy, gs, ops, tgt, newh, 
                              prev, prunedNow >>

WEnd(self) == /\ pc[self] = "WEnd"
              /\ busy' = [busy EXCEPT ![self] = FALSE]
              /\ pc' = [pc EXCEPT ![self] = "WLoop"]
              /\ UNCHANGED << now, blobE, born, head, vrefs, live, markT, cand, 
                              seenBorn, ws, gbusy, gs, ops, tgt, newh, prev, 
                              prunedNow >>

w(self) == WLoop(self) \/ WPut(self) \/ WTx(self) \/ WClean(self)
              \/ WDel(self) \/ WEnd(self)

GRound == /\ pc["GC"] = "GRound"
          /\ gbusy' = TRUE
          /\ gs' = now
          /\ pc' = [pc EXCEPT !["GC"] = "GMark"]
          /\ UNCHANGED << now, blobE, born, head, vrefs, live, markT, cand, 
                          seenBorn, busy, ws, ops, tgt, newh, prev, prunedNow >>

GMark == /\ pc["GC"] = "GMark"
         /\ live' = AllRefs
         /\ markT' = now
         /\ pc' = [pc EXCEPT !["GC"] = "GList"]
         /\ UNCHANGED << now, blobE, born, head, vrefs, cand, seenBorn, busy, 
                         ws, gbusy, gs, ops, tgt, newh, prev, prunedNow >>

GList == /\ pc["GC"] = "GList"
         /\ cand' = [h \in Hashes |->
                       /\ blobE[h]
                       /\ h \notin live
                       /\ born[h] + MinAge < markT]
         /\ seenBorn' = born
         /\ pc' = [pc EXCEPT !["GC"] = "GDel"]
         /\ UNCHANGED << now, blobE, born, head, vrefs, live, markT, busy, ws, 
                         gbusy, gs, ops, tgt, newh, prev, prunedNow >>

GDel == /\ pc["GC"] = "GDel"
        /\ blobE' = [h \in Hashes |->
                       IF /\ cand[h]
                          /\ (~SweepPinsGeneration \/ born[h] = seenBorn[h])
                       THEN FALSE
                       ELSE blobE[h]]
        /\ gbusy' = FALSE
        /\ pc' = [pc EXCEPT !["GC"] = "GRound"]
        /\ UNCHANGED << now, born, head, vrefs, live, markT, cand, seenBorn, 
                        busy, ws, gs, ops, tgt, newh, prev, prunedNow >>

gc == GRound \/ GMark \/ GList \/ GDel

Tick == /\ pc["CLK"] = "Tick"
        /\ IF now < MaxClock
              THEN /\ /\ \A p \in Writers : ~busy[p] \/ now <= ws[p]
                      /\ (~gbusy \/ now <= gs)
                   /\ now' = now + 1
                   /\ pc' = [pc EXCEPT !["CLK"] = "Tick"]
              ELSE /\ pc' = [pc EXCEPT !["CLK"] = "Done"]
                   /\ now' = now
        /\ UNCHANGED << blobE, born, head, vrefs, live, markT, cand, seenBorn, 
                        busy, ws, gbusy, gs, ops, tgt, newh, prev, prunedNow >>

clk == Tick

Next == gc \/ clk
           \/ (\E self \in Writers: w(self))

Spec == Init /\ [][Next]_vars

\* END TRANSLATION 
\* Symmetry: writers, artifact slots, and content hashes are interchangeable
\* model values -- checking one representative of each permutation class is
\* sound for the NoDangling invariant and shrinks the state space ~8x.
Symm == Permutations(Writers) \cup Permutations(Arts) \cup Permutations(Hashes)

=============================================================================

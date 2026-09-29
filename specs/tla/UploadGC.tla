------------------------------ MODULE UploadGC ------------------------------
(***************************************************************************)
(* The large-upload commit protocol vs. the GC sweeper.                    *)
(*                                                                         *)
(* Models the interaction between:                                         *)
(*   - `core/uploads.py` commit_upload: expiry check -> GCS stat ->        *)
(*     artifact upsert -> settle (open->committed + reservation release),  *)
(*     each in its OWN transaction, under a per-upload advisory lock       *)
(*     ('upload-commit:{id}') that serializes commits/aborts of the same   *)
(*     upload but is NOT taken by the GC.                                  *)
(*   - `core/gc.py` sweep: release_expired (bulk DELETE of open+expired    *)
(*     pending_uploads rows) -> mark (live_set = artifact/version blob     *)
(*     pointers UNION open pending object_keys) -> sweep (delete blobs     *)
(*     not in live_set older than MARK_SWEEP_AGE).                         *)
(*                                                                         *)
(* Time is a discrete clock. One tick ~ MARK_SWEEP_AGE (24h); the session  *)
(* TTL is TTL ticks (~6 days in prod). The uploaded blob's age is decoupled*)
(* from the session's expiry: bytes may be PUT on day 0 and committed on   *)
(* day 6, so the blob can be far older than MARK_SWEEP_AGE while its       *)
(* commit is still legitimately in flight. The pending_uploads 'open' row  *)
(* is what protects it from the mark-sweep -- and release_expired removes  *)
(* that protection without honoring the commit advisory lock.             *)
(*                                                                         *)
(* GcLocksRow = FALSE models the code as shipped.                          *)
(* GcLocksRow = TRUE models the candidate fix: release_expired takes       *)
(* pg_try_advisory_lock('upload-commit:{id}') per row and skips rows whose *)
(* lock is held (an in-flight commit/abort), reaping them next sweep.      *)
(*                                                                         *)
(* Invariant NoDangling: no artifact/version row ever references a GCS     *)
(* object that does not exist.                                             *)
(*                                                                         *)
(* Simplifications (safe for this invariant):                              *)
(*   - abort_upload is not modeled: it holds the same advisory lock as     *)
(*     commit, so within one upload the two cannot interleave; abort only  *)
(*     removes states.                                                     *)
(*   - The commit idempotency guard (artifact_versions.gcs_object lookup)  *)
(*     is not modeled: it only matters for crash-retried commits, which    *)
(*     we don't model. Each upload commits at most once.                   *)
(*   - The mark phase's two queries (artifact pointers, open pending rows) *)
(*     are one atomic snapshot here. That is charitable to the code; the   *)
(*     violation found does not depend on splitting them.                  *)
(*   - reserved_bytes accounting is out of scope (a future spec).          *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
  Uploads,     \* model values: the upload sessions, e.g. {u1}
  TTL,         \* session lifetime in ticks (expires_at = begin + TTL)
  MinAge,      \* MARK_SWEEP_AGE in ticks: blob deletable only if born + MinAge < now
  MaxClock,    \* clock bound
  GcLocksRow   \* TRUE = release_expired honors the per-upload advisory lock

(* --algorithm uploadgc

variables
  now    = 0,
  \* pending_uploads.state: "none" (no row yet) | "open" | "committed" | "gone"
  \* ("gone" = row DELETEd by release_expired)
  pstate = [u \in Uploads |-> "none"],
  expiry = [u \in Uploads |-> 0],
  \* the GCS object at obj/{drive}/{upload_id}
  blob   = [u \in Uploads |-> FALSE],
  born   = [u \in Uploads |-> 0],
  \* uploads whose object_key is referenced from artifacts/artifact_versions
  arts   = {},
  \* the 'upload-commit:{id}' advisory lock
  lock   = [u \in Uploads |-> FALSE],
  \* the GC's mark snapshot + the clock value it was taken at. The code
  \* freezes `cutoff` at phase start (gc.py:898) and live_set at mark time
  \* (gc.py:937-958); sweep decisions never re-read the clock. Modeling
  \* the age gate against `now` instead of `markT` would let a mark
  \* snapshot go stale across ticks (~days) -- impossible under the
  \* sweeper's 50-minute hard timeout -- and yields spurious violations.
  live   = {},
  markT  = 0;

define
  NoDangling == \A u \in arts : blob[u]
end define;

\* ------------------------- uploader, one per session -------------------------
process up \in Uploads
begin
Begin:            \* begin_upload: reserve + INSERT open row (one tx)
  pstate[self] := "open";
  expiry[self] := now + TTL;
Put:              \* client PUTs the bytes; object exists from here
  blob[self] := TRUE;
  born[self] := now;
CLock:            \* commit_upload: pg_advisory_lock('upload-commit:{id}')
  lock[self] := TRUE;
CChk:             \* read row; reject committed/aborted/expired  (uploads.py:379-394)
  if pstate[self] /= "open" \/ expiry[self] < now then
    lock[self] := FALSE;
    goto Fin;
  end if;
CStat:            \* storage.stat(object_key)                    (uploads.py:401)
  if ~blob[self] then
    lock[self] := FALSE;
    goto Fin;
  end if;
CUpsert:          \* upsert_artifact_prewritten: version row now references the blob
  arts := arts \cup {self};
CSettle:          \* flip open->committed WHERE state='open' + release reservation
  if pstate[self] = "open" then
    pstate[self] := "committed";
  end if;
  lock[self] := FALSE;
Fin:
  skip;
end process;

\* ------------------------------- GC sweeper ---------------------------------
process gc = "GC"
begin
GRel:             \* release_expired: DELETE open+expired rows (uploads.py:465)
  while TRUE do
    pstate := [u \in Uploads |->
                 IF /\ pstate[u] = "open"
                    /\ expiry[u] < now
                    /\ (~GcLocksRow \/ ~lock[u])
                 THEN "gone"
                 ELSE pstate[u]];
GMark:            \* live_set := artifact pointers UNION open pending keys (gc.py:937-958)
    live := arts \cup {u \in Uploads : pstate[u] = "open"};
    markT := now;
GSweep:           \* delete unreferenced blobs older than the FROZEN cutoff (gc.py:969-1011)
    blob := [u \in Uploads |->
               IF /\ blob[u]
                  /\ u \notin live
                  /\ born[u] + MinAge < markT
               THEN FALSE
               ELSE blob[u]];
  end while;
end process;

\* --------------------------------- clock ------------------------------------
process clk = "CLK"
begin
Tick:
  while now < MaxClock do
    now := now + 1;
  end while;
end process;

end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "26c95a4d" /\ chksum(tla) = "97d1b216")
VARIABLES now, pstate, expiry, blob, born, arts, lock, live, markT, pc

(* define statement *)
NoDangling == \A u \in arts : blob[u]


vars == << now, pstate, expiry, blob, born, arts, lock, live, markT, pc >>

ProcSet == (Uploads) \cup {"GC"} \cup {"CLK"}

Init == (* Global variables *)
        /\ now = 0
        /\ pstate = [u \in Uploads |-> "none"]
        /\ expiry = [u \in Uploads |-> 0]
        /\ blob = [u \in Uploads |-> FALSE]
        /\ born = [u \in Uploads |-> 0]
        /\ arts = {}
        /\ lock = [u \in Uploads |-> FALSE]
        /\ live = {}
        /\ markT = 0
        /\ pc = [self \in ProcSet |-> CASE self \in Uploads -> "Begin"
                                        [] self = "GC" -> "GRel"
                                        [] self = "CLK" -> "Tick"]

Begin(self) == /\ pc[self] = "Begin"
               /\ pstate' = [pstate EXCEPT ![self] = "open"]
               /\ expiry' = [expiry EXCEPT ![self] = now + TTL]
               /\ pc' = [pc EXCEPT ![self] = "Put"]
               /\ UNCHANGED << now, blob, born, arts, lock, live, markT >>

Put(self) == /\ pc[self] = "Put"
             /\ blob' = [blob EXCEPT ![self] = TRUE]
             /\ born' = [born EXCEPT ![self] = now]
             /\ pc' = [pc EXCEPT ![self] = "CLock"]
             /\ UNCHANGED << now, pstate, expiry, arts, lock, live, markT >>

CLock(self) == /\ pc[self] = "CLock"
               /\ lock' = [lock EXCEPT ![self] = TRUE]
               /\ pc' = [pc EXCEPT ![self] = "CChk"]
               /\ UNCHANGED << now, pstate, expiry, blob, born, arts, live, 
                               markT >>

CChk(self) == /\ pc[self] = "CChk"
              /\ IF pstate[self] /= "open" \/ expiry[self] < now
                    THEN /\ lock' = [lock EXCEPT ![self] = FALSE]
                         /\ pc' = [pc EXCEPT ![self] = "Fin"]
                    ELSE /\ pc' = [pc EXCEPT ![self] = "CStat"]
                         /\ lock' = lock
              /\ UNCHANGED << now, pstate, expiry, blob, born, arts, live, 
                              markT >>

CStat(self) == /\ pc[self] = "CStat"
               /\ IF ~blob[self]
                     THEN /\ lock' = [lock EXCEPT ![self] = FALSE]
                          /\ pc' = [pc EXCEPT ![self] = "Fin"]
                     ELSE /\ pc' = [pc EXCEPT ![self] = "CUpsert"]
                          /\ lock' = lock
               /\ UNCHANGED << now, pstate, expiry, blob, born, arts, live, 
                               markT >>

CUpsert(self) == /\ pc[self] = "CUpsert"
                 /\ arts' = (arts \cup {self})
                 /\ pc' = [pc EXCEPT ![self] = "CSettle"]
                 /\ UNCHANGED << now, pstate, expiry, blob, born, lock, live, 
                                 markT >>

CSettle(self) == /\ pc[self] = "CSettle"
                 /\ IF pstate[self] = "open"
                       THEN /\ pstate' = [pstate EXCEPT ![self] = "committed"]
                       ELSE /\ TRUE
                            /\ UNCHANGED pstate
                 /\ lock' = [lock EXCEPT ![self] = FALSE]
                 /\ pc' = [pc EXCEPT ![self] = "Fin"]
                 /\ UNCHANGED << now, expiry, blob, born, arts, live, markT >>

Fin(self) == /\ pc[self] = "Fin"
             /\ TRUE
             /\ pc' = [pc EXCEPT ![self] = "Done"]
             /\ UNCHANGED << now, pstate, expiry, blob, born, arts, lock, live, 
                             markT >>

up(self) == Begin(self) \/ Put(self) \/ CLock(self) \/ CChk(self)
               \/ CStat(self) \/ CUpsert(self) \/ CSettle(self)
               \/ Fin(self)

GRel == /\ pc["GC"] = "GRel"
        /\ pstate' = [u \in Uploads |->
                        IF /\ pstate[u] = "open"
                           /\ expiry[u] < now
                           /\ (~GcLocksRow \/ ~lock[u])
                        THEN "gone"
                        ELSE pstate[u]]
        /\ pc' = [pc EXCEPT !["GC"] = "GMark"]
        /\ UNCHANGED << now, expiry, blob, born, arts, lock, live, markT >>

GMark == /\ pc["GC"] = "GMark"
         /\ live' = (arts \cup {u \in Uploads : pstate[u] = "open"})
         /\ markT' = now
         /\ pc' = [pc EXCEPT !["GC"] = "GSweep"]
         /\ UNCHANGED << now, pstate, expiry, blob, born, arts, lock >>

GSweep == /\ pc["GC"] = "GSweep"
          /\ blob' = [u \in Uploads |->
                        IF /\ blob[u]
                           /\ u \notin live
                           /\ born[u] + MinAge < markT
                        THEN FALSE
                        ELSE blob[u]]
          /\ pc' = [pc EXCEPT !["GC"] = "GRel"]
          /\ UNCHANGED << now, pstate, expiry, born, arts, lock, live, markT >>

gc == GRel \/ GMark \/ GSweep

Tick == /\ pc["CLK"] = "Tick"
        /\ IF now < MaxClock
              THEN /\ now' = now + 1
                   /\ pc' = [pc EXCEPT !["CLK"] = "Tick"]
              ELSE /\ pc' = [pc EXCEPT !["CLK"] = "Done"]
                   /\ now' = now
        /\ UNCHANGED << pstate, expiry, blob, born, arts, lock, live, markT >>

clk == Tick

Next == gc \/ clk
           \/ (\E self \in Uploads: up(self))

Spec == Init /\ [][Next]_vars

\* END TRANSLATION 
=============================================================================

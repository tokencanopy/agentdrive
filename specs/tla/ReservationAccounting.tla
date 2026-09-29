------------------------ MODULE ReservationAccounting ------------------------
(***************************************************************************)
(* The large-upload reservation ledger (core/uploads.py).                  *)
(*                                                                         *)
(* `begin_upload` reserves quota on `drives.reserved_bytes`; exactly one   *)
(* of three paths must release it, exactly once: commit's settle           *)
(* (open->committed), abort (open->aborted, unexpired only), or the GC's   *)
(* release_expired (row DELETE, post-PR#351: skips rows whose commit       *)
(* advisory lock is held). The code clamps releases with GREATEST(..,0),   *)
(* which would MASK a double-release by silently under-counting other      *)
(* sessions' reservations — so this model keeps `reserved` unclamped and   *)
(* checks exact accounting instead:                                        *)
(*                                                                         *)
(*   Accounting == reserved = number of open sessions                      *)
(*                                                                         *)
(* (all sessions have model size 1). This catches double releases,        *)
(* missed releases, and releases against the wrong lifecycle state.        *)
(*                                                                         *)
(* What makes this worth model-checking is the CRASH-RETRY protocol:       *)
(* commit's artifact write and its settle are separate transactions, and   *)
(* the process can crash between any two steps — dropping its session-     *)
(* scoped advisory lock — then retry from the top. The retry paths         *)
(* (state=committed -> return; artifact-already-written -> skip upsert,    *)
(* settle anyway; row swept -> rejected) must still release exactly once.  *)
(* Abort interleaves under the same lock; expired-but-unswept aborts are   *)
(* deliberate no-ops (GC owns that release — uploads.py:206).              *)
(*                                                                         *)
(* Blob existence / mark-sweep are out of scope here (UploadGC.tla and     *)
(* ArtifactGC.tla own the NoDangling side).                                *)
(*                                                                         *)
(* Expected: clean — this spec is verification, not a bug reproduction;    *)
(* it pins the accounting protocol against future refactors.               *)
(***************************************************************************)
EXTENDS Naturals, Integers, FiniteSets

CONSTANTS
  Uploads,   \* model values: upload sessions (one drive)
  TTL,       \* session lifetime in ticks
  MaxClock,  \* clock bound
  MaxTries   \* commit/abort attempts per session (bounds crash-retries)

(* --algorithm reservations

variables
  now      = 0,
  \* pending_uploads.state: none | open | committed | aborted | gone (DELETEd)
  state    = [u \in Uploads |-> "none"],
  expiry   = [u \in Uploads |-> 0],
  \* drives.reserved_bytes, UNCLAMPED (each session reserves 1)
  reserved = 0,
  \* the 'upload-commit:{id}' advisory lock (held by commit AND abort)
  lock     = [u \in Uploads |-> FALSE];

define
  Accounting == reserved = Cardinality({u \in Uploads : state[u] = "open"})
end define;

\* --------------------- one controller per session ---------------------------
process up \in Uploads
variables tries = 0, wrote = FALSE;  \* wrote = the artifact_versions row exists
begin
UBegin:        \* begin_upload: reserve + INSERT 'open' row, one tx
  state[self] := "open";
  expiry[self] := now + TTL;
  reserved := reserved + 1;
ULife:
  while tries < MaxTries do
    tries := tries + 1;
    either
      \* ---------------- commit attempt (may crash mid-flight) --------------
CLock:
      lock[self] := TRUE;
CChk:          \* read row; committed -> idempotent return; aborted/gone/
               \* expired -> rejected (no release here)  (uploads.py:379-394)
      if state[self] = "committed" then
        lock[self] := FALSE;
        goto UDone;
      elsif state[self] /= "open" \/ expiry[self] < now then
        lock[self] := FALSE;
        goto ULife;
      end if;
CCrash1:       \* connection may drop before the artifact write —
               \* the session-scoped advisory lock drops with it
      either goto CCrashed; or skip; end either;
CUpsert:       \* upsert_artifact_prewritten, guarded by the object-key
               \* idempotency check (a crash-retry never writes twice)
      if ~wrote then
        wrote := TRUE;
      end if;
CCrash2:       \* the dangerous window: artifact written, settle not yet run
      either goto CCrashed; or skip; end either;
CSettle:       \* flip open->committed + release, one tx, WHERE state='open'
      if state[self] = "open" then
        state[self] := "committed";
        reserved := reserved - 1;
      end if;
      lock[self] := FALSE;
      goto UDone;
CCrashed:
      lock[self] := FALSE;
    or
      \* ---------------- abort attempt (same advisory lock) -----------------
ALock:
      lock[self] := TRUE;
AChk:          \* live open -> release + flip; expired open -> no-op (GC owns
               \* the release); committed/aborted/gone -> no-op (uploads.py:185)
      if state[self] = "open" /\ expiry[self] >= now then
        state[self] := "aborted";
        reserved := reserved - 1;
      end if;
      lock[self] := FALSE;
    end either;
  end while;
UDone:
  skip;
end process;

\* ------------------------------- GC sweeper ---------------------------------
process gc = "GC"
begin
GRel:          \* release_expired (post-PR#351): DELETE open+expired rows whose
               \* commit lock is NOT held; release their reservations. One tx.
  while TRUE do
    reserved := reserved - Cardinality(
      {u \in Uploads : state[u] = "open" /\ expiry[u] < now /\ ~lock[u]});
    state := [u \in Uploads |->
                IF state[u] = "open" /\ expiry[u] < now /\ ~lock[u]
                THEN "gone"
                ELSE state[u]];
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
\* BEGIN TRANSLATION (chksum(pcal) = "176a4163" /\ chksum(tla) = "3bdf42af")
VARIABLES now, state, expiry, reserved, lock, pc

(* define statement *)
Accounting == reserved = Cardinality({u \in Uploads : state[u] = "open"})

VARIABLES tries, wrote

vars == << now, state, expiry, reserved, lock, pc, tries, wrote >>

ProcSet == (Uploads) \cup {"GC"} \cup {"CLK"}

Init == (* Global variables *)
        /\ now = 0
        /\ state = [u \in Uploads |-> "none"]
        /\ expiry = [u \in Uploads |-> 0]
        /\ reserved = 0
        /\ lock = [u \in Uploads |-> FALSE]
        (* Process up *)
        /\ tries = [self \in Uploads |-> 0]
        /\ wrote = [self \in Uploads |-> FALSE]
        /\ pc = [self \in ProcSet |-> CASE self \in Uploads -> "UBegin"
                                        [] self = "GC" -> "GRel"
                                        [] self = "CLK" -> "Tick"]

UBegin(self) == /\ pc[self] = "UBegin"
                /\ state' = [state EXCEPT ![self] = "open"]
                /\ expiry' = [expiry EXCEPT ![self] = now + TTL]
                /\ reserved' = reserved + 1
                /\ pc' = [pc EXCEPT ![self] = "ULife"]
                /\ UNCHANGED << now, lock, tries, wrote >>

ULife(self) == /\ pc[self] = "ULife"
               /\ IF tries[self] < MaxTries
                     THEN /\ tries' = [tries EXCEPT ![self] = tries[self] + 1]
                          /\ \/ /\ pc' = [pc EXCEPT ![self] = "CLock"]
                             \/ /\ pc' = [pc EXCEPT ![self] = "ALock"]
                     ELSE /\ pc' = [pc EXCEPT ![self] = "UDone"]
                          /\ tries' = tries
               /\ UNCHANGED << now, state, expiry, reserved, lock, wrote >>

CLock(self) == /\ pc[self] = "CLock"
               /\ lock' = [lock EXCEPT ![self] = TRUE]
               /\ pc' = [pc EXCEPT ![self] = "CChk"]
               /\ UNCHANGED << now, state, expiry, reserved, tries, wrote >>

CChk(self) == /\ pc[self] = "CChk"
              /\ IF state[self] = "committed"
                    THEN /\ lock' = [lock EXCEPT ![self] = FALSE]
                         /\ pc' = [pc EXCEPT ![self] = "UDone"]
                    ELSE /\ IF state[self] /= "open" \/ expiry[self] < now
                               THEN /\ lock' = [lock EXCEPT ![self] = FALSE]
                                    /\ pc' = [pc EXCEPT ![self] = "ULife"]
                               ELSE /\ pc' = [pc EXCEPT ![self] = "CCrash1"]
                                    /\ lock' = lock
              /\ UNCHANGED << now, state, expiry, reserved, tries, wrote >>

CCrash1(self) == /\ pc[self] = "CCrash1"
                 /\ \/ /\ pc' = [pc EXCEPT ![self] = "CCrashed"]
                    \/ /\ TRUE
                       /\ pc' = [pc EXCEPT ![self] = "CUpsert"]
                 /\ UNCHANGED << now, state, expiry, reserved, lock, tries, 
                                 wrote >>

CUpsert(self) == /\ pc[self] = "CUpsert"
                 /\ IF ~wrote[self]
                       THEN /\ wrote' = [wrote EXCEPT ![self] = TRUE]
                       ELSE /\ TRUE
                            /\ wrote' = wrote
                 /\ pc' = [pc EXCEPT ![self] = "CCrash2"]
                 /\ UNCHANGED << now, state, expiry, reserved, lock, tries >>

CCrash2(self) == /\ pc[self] = "CCrash2"
                 /\ \/ /\ pc' = [pc EXCEPT ![self] = "CCrashed"]
                    \/ /\ TRUE
                       /\ pc' = [pc EXCEPT ![self] = "CSettle"]
                 /\ UNCHANGED << now, state, expiry, reserved, lock, tries, 
                                 wrote >>

CSettle(self) == /\ pc[self] = "CSettle"
                 /\ IF state[self] = "open"
                       THEN /\ state' = [state EXCEPT ![self] = "committed"]
                            /\ reserved' = reserved - 1
                       ELSE /\ TRUE
                            /\ UNCHANGED << state, reserved >>
                 /\ lock' = [lock EXCEPT ![self] = FALSE]
                 /\ pc' = [pc EXCEPT ![self] = "UDone"]
                 /\ UNCHANGED << now, expiry, tries, wrote >>

CCrashed(self) == /\ pc[self] = "CCrashed"
                  /\ lock' = [lock EXCEPT ![self] = FALSE]
                  /\ pc' = [pc EXCEPT ![self] = "ULife"]
                  /\ UNCHANGED << now, state, expiry, reserved, tries, wrote >>

ALock(self) == /\ pc[self] = "ALock"
               /\ lock' = [lock EXCEPT ![self] = TRUE]
               /\ pc' = [pc EXCEPT ![self] = "AChk"]
               /\ UNCHANGED << now, state, expiry, reserved, tries, wrote >>

AChk(self) == /\ pc[self] = "AChk"
              /\ IF state[self] = "open" /\ expiry[self] >= now
                    THEN /\ state' = [state EXCEPT ![self] = "aborted"]
                         /\ reserved' = reserved - 1
                    ELSE /\ TRUE
                         /\ UNCHANGED << state, reserved >>
              /\ lock' = [lock EXCEPT ![self] = FALSE]
              /\ pc' = [pc EXCEPT ![self] = "ULife"]
              /\ UNCHANGED << now, expiry, tries, wrote >>

UDone(self) == /\ pc[self] = "UDone"
               /\ TRUE
               /\ pc' = [pc EXCEPT ![self] = "Done"]
               /\ UNCHANGED << now, state, expiry, reserved, lock, tries, 
                               wrote >>

up(self) == UBegin(self) \/ ULife(self) \/ CLock(self) \/ CChk(self)
               \/ CCrash1(self) \/ CUpsert(self) \/ CCrash2(self)
               \/ CSettle(self) \/ CCrashed(self) \/ ALock(self)
               \/ AChk(self) \/ UDone(self)

GRel == /\ pc["GC"] = "GRel"
        /\ reserved' =           reserved - Cardinality(
                       {u \in Uploads : state[u] = "open" /\ expiry[u] < now /\ ~lock[u]})
        /\ state' = [u \in Uploads |->
                       IF state[u] = "open" /\ expiry[u] < now /\ ~lock[u]
                       THEN "gone"
                       ELSE state[u]]
        /\ pc' = [pc EXCEPT !["GC"] = "GRel"]
        /\ UNCHANGED << now, expiry, lock, tries, wrote >>

gc == GRel

Tick == /\ pc["CLK"] = "Tick"
        /\ IF now < MaxClock
              THEN /\ now' = now + 1
                   /\ pc' = [pc EXCEPT !["CLK"] = "Tick"]
              ELSE /\ pc' = [pc EXCEPT !["CLK"] = "Done"]
                   /\ now' = now
        /\ UNCHANGED << state, expiry, reserved, lock, tries, wrote >>

clk == Tick

Next == gc \/ clk
           \/ (\E self \in Uploads: up(self))

Spec == Init /\ [][Next]_vars

\* END TRANSLATION 
=============================================================================

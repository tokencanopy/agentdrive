--------------------------- MODULE FolderNamespace ---------------------------
(***************************************************************************)
(* Folder operations vs. concurrent artifact writes: the file-vs-folder    *)
(* name-exclusivity invariant.                                             *)
(*                                                                         *)
(* folders-and-permalinks-design.md declares ("avoid file-vs-folder        *)
(* ambiguity at the same name", §4.9/§5): a live folder `/X/` and a live   *)
(* artifact `X` must never coexist. `mkdir` enforces one direction with a  *)
(* pre-INSERT probe of `artifacts` (folders.py `mkdir`, "file-form         *)
(* collision check") — but that probe is a plain SELECT with no lock       *)
(* shared with the artifact write path, and the artifact side              *)
(* (`upsert_artifact`, plus rename/copy/restore destinations) checks      *)
(* nothing at all.                                                         *)
(*                                                                         *)
(* What is deliberately NOT modeled — the design declares these legal, so  *)
(* they are not invariant material (§5 edge table):                        *)
(*   - writes into a soft-deleted folder's path prefix ("folders are      *)
(*     sparse metadata", no FK);                                           *)
(*   - artifacts left under a moved folder's source prefix (move locks     *)
(*     only the destination prefix, §13.5);                                *)
(*   - mkdir under/over existing artifacts at OTHER paths.                 *)
(*   - move/delete cascades: single-tx under the per-drive cascade         *)
(*     advisory lock; their dest-collision handling has its own §13.5      *)
(*     locking discipline. Out of scope here.                              *)
(*                                                                         *)
(* Two toggles model three protocol variants:                              *)
(*   UpsertChecksFolders: the artifact write path probes for a live        *)
(*     folder at its path's folder form (the missing mirror check).        *)
(*   MkdirLocksDrive: mkdir takes the drives-row FOR UPDATE (which the     *)
(*     artifact write path ALREADY holds across its commit tx for quota    *)
(*     accounting) — giving both probes a common serialization point.      *)
(*                                                                         *)
(*   _bug   (F,F): as shipped — violated SERIALLY (mkdir /X/, then        *)
(*                 upsert X succeeds; empirically confirmed).              *)
(*   _probe (T,F): the naive symmetric-probe fix — still violated by the   *)
(*                 RACE: both unlocked probes pass before either insert    *)
(*                 commits. The probe alone is not a fix.                  *)
(*   _fix   (T,T): probe + insert on both sides under the drive row lock   *)
(*                 — checks clean. This is the recommended protocol.       *)
(*                                                                         *)
(* Invariant NoShadow: no name has a live folder and a live artifact       *)
(* simultaneously.                                                         *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
  Workers,             \* model values: concurrent API callers
  Names,               \* model values: path names (folder form /n/, file form n)
  MaxOps,              \* ops per worker
  UpsertChecksFolders, \* artifact write probes for a live folder (mirror check)
  MkdirLocksDrive      \* mkdir serializes on the drives-row lock like upsert

(* --algorithm folderns

variables
  folderLive = [n \in Names |-> FALSE],  \* live `folders` row at /n/
  artLive    = [n \in Names |-> FALSE],  \* live `artifacts` row at n
  \* the drives-row FOR UPDATE: upsert holds it across its commit tx
  \* (quota accounting); mkdir holds it only under MkdirLocksDrive.
  driveLk    = FALSE;

define
  NoShadow == \A n \in Names : ~(folderLive[n] /\ artLive[n])
end define;

process w \in Workers
variables ops = 0, tgt = CHOOSE n \in Names : TRUE;
begin
WLoop:
  while ops < MaxOps do
    ops := ops + 1;
    with n \in Names do tgt := n; end with;
    either
      \* ------------------------- mkdir /tgt/ ------------------------------
MLock:
      if MkdirLocksDrive then
        await ~driveLk;
        driveLk := TRUE;
      end if;
MProbe:   \* file-form collision check: plain SELECT of committed state
      if artLive[tgt] then
        if MkdirLocksDrive then driveLk := FALSE; end if;
        goto WLoop;      \* FOLDER_PATH_CONFLICT -> 409
      end if;
MIns:     \* INSERT (partial unique index: live-row collision -> idempotent
          \* return, same outcome for this invariant)
      folderLive[tgt] := TRUE;
      if MkdirLocksDrive then driveLk := FALSE; end if;
    or
      \* --------------- artifact write at file form tgt ---------------------
      \* Stands in for every dest-path mutator: upsert, rename/copy/restore
      \* destinations. All hold the drives-row lock across their commit tx.
ALock:
      await ~driveLk;
      driveLk := TRUE;
AProbe:   \* the mirror check (only under UpsertChecksFolders)
      if UpsertChecksFolders /\ folderLive[tgt] then
        driveLk := FALSE;
        goto WLoop;      \* would be a 409 folder-form conflict
      end if;
AIns:     \* INSERT/overwrite; no cross-table constraint exists in the schema
      artLive[tgt] := TRUE;
      driveLk := FALSE;
    end either;
  end while;
end process;

end algorithm; *)
\* BEGIN TRANSLATION (chksum(pcal) = "b92f0f20" /\ chksum(tla) = "a83280af")
VARIABLES folderLive, artLive, driveLk, pc

(* define statement *)
NoShadow == \A n \in Names : ~(folderLive[n] /\ artLive[n])

VARIABLES ops, tgt

vars == << folderLive, artLive, driveLk, pc, ops, tgt >>

ProcSet == (Workers)

Init == (* Global variables *)
        /\ folderLive = [n \in Names |-> FALSE]
        /\ artLive = [n \in Names |-> FALSE]
        /\ driveLk = FALSE
        (* Process w *)
        /\ ops = [self \in Workers |-> 0]
        /\ tgt = [self \in Workers |-> CHOOSE n \in Names : TRUE]
        /\ pc = [self \in ProcSet |-> "WLoop"]

WLoop(self) == /\ pc[self] = "WLoop"
               /\ IF ops[self] < MaxOps
                     THEN /\ ops' = [ops EXCEPT ![self] = ops[self] + 1]
                          /\ \E n \in Names:
                               tgt' = [tgt EXCEPT ![self] = n]
                          /\ \/ /\ pc' = [pc EXCEPT ![self] = "MLock"]
                             \/ /\ pc' = [pc EXCEPT ![self] = "ALock"]
                     ELSE /\ pc' = [pc EXCEPT ![self] = "Done"]
                          /\ UNCHANGED << ops, tgt >>
               /\ UNCHANGED << folderLive, artLive, driveLk >>

MLock(self) == /\ pc[self] = "MLock"
               /\ IF MkdirLocksDrive
                     THEN /\ ~driveLk
                          /\ driveLk' = TRUE
                     ELSE /\ TRUE
                          /\ UNCHANGED driveLk
               /\ pc' = [pc EXCEPT ![self] = "MProbe"]
               /\ UNCHANGED << folderLive, artLive, ops, tgt >>

MProbe(self) == /\ pc[self] = "MProbe"
                /\ IF artLive[tgt[self]]
                      THEN /\ IF MkdirLocksDrive
                                 THEN /\ driveLk' = FALSE
                                 ELSE /\ TRUE
                                      /\ UNCHANGED driveLk
                           /\ pc' = [pc EXCEPT ![self] = "WLoop"]
                      ELSE /\ pc' = [pc EXCEPT ![self] = "MIns"]
                           /\ UNCHANGED driveLk
                /\ UNCHANGED << folderLive, artLive, ops, tgt >>

MIns(self) == /\ pc[self] = "MIns"
              /\ folderLive' = [folderLive EXCEPT ![tgt[self]] = TRUE]
              /\ IF MkdirLocksDrive
                    THEN /\ driveLk' = FALSE
                    ELSE /\ TRUE
                         /\ UNCHANGED driveLk
              /\ pc' = [pc EXCEPT ![self] = "WLoop"]
              /\ UNCHANGED << artLive, ops, tgt >>

ALock(self) == /\ pc[self] = "ALock"
               /\ ~driveLk
               /\ driveLk' = TRUE
               /\ pc' = [pc EXCEPT ![self] = "AProbe"]
               /\ UNCHANGED << folderLive, artLive, ops, tgt >>

AProbe(self) == /\ pc[self] = "AProbe"
                /\ IF UpsertChecksFolders /\ folderLive[tgt[self]]
                      THEN /\ driveLk' = FALSE
                           /\ pc' = [pc EXCEPT ![self] = "WLoop"]
                      ELSE /\ pc' = [pc EXCEPT ![self] = "AIns"]
                           /\ UNCHANGED driveLk
                /\ UNCHANGED << folderLive, artLive, ops, tgt >>

AIns(self) == /\ pc[self] = "AIns"
              /\ artLive' = [artLive EXCEPT ![tgt[self]] = TRUE]
              /\ driveLk' = FALSE
              /\ pc' = [pc EXCEPT ![self] = "WLoop"]
              /\ UNCHANGED << folderLive, ops, tgt >>

w(self) == WLoop(self) \/ MLock(self) \/ MProbe(self) \/ MIns(self)
              \/ ALock(self) \/ AProbe(self) \/ AIns(self)

(* Allow infinite stuttering to prevent deadlock on termination. *)
Terminating == /\ \A self \in ProcSet: pc[self] = "Done"
               /\ UNCHANGED vars

Next == (\E self \in Workers: w(self))
           \/ Terminating

Spec == Init /\ [][Next]_vars

Termination == <>(\A self \in ProcSet: pc[self] = "Done")

\* END TRANSLATION 
=============================================================================

import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type { Job } from "@/api/types";

/**
 * Selection and transient UI state. Server data lives in RTK Query's cache —
 * duplicating it here is how two sources of truth start disagreeing.
 */
export interface UiState {
  workspace: string | null;
  table: string | null;
  search: string;
  reviewer: string;
  runningJobId: string | null;
  runningWorkspace: string | null;
  /**
   * The last run that ended, kept after `runningJobId` clears.
   *
   * Without it the status vanished the instant a job finished, so a run that
   * took ten minutes ended with no acknowledgement that it had done anything.
   */
  finishedJob: Job | null;
  view: "workspace" | "map" | "questions" | "sources";
  /**
   * Whether the map's semantic-view panel is open.
   *
   * Here rather than in the component because the map unmounts on every view
   * switch. Closing it to read the graph and finding it reopened on the way
   * back is the kind of thing that makes a panel feel like it is fighting you.
   */
  mapPaneOpen: boolean;
  /**
   * The relationship under review on the map, by edge id.
   *
   * Separate from `table` because they are different questions. A table
   * selection asks what an agent would be given for it; an edge selection asks
   * whether this relationship is real — and only one of them can be answered
   * by looking at a picture.
   */
  edge: string | null;
}

const initialState: UiState = {
  workspace: null,
  table: null,
  search: "",
  // No auth yet, so the reviewer is self-declared. It is still recorded on
  // every verdict, because an approval with no name attached is not a review.
  reviewer: "you",
  runningJobId: null,
  runningWorkspace: null,
  finishedJob: null,
  view: "workspace",
  // Open to start with: it is the artifact the map exists to explain, and a
  // panel nobody knows is there is a feature nobody uses.
  mapPaneOpen: true,
  edge: null,
};

const uiSlice = createSlice({
  name: "ui",
  initialState,
  reducers: {
    selectWorkspace(state, action: PayloadAction<string>) {
      state.workspace = action.payload;
      state.table = null;
      state.edge = null;
    },
    clearWorkspace(state) {
      state.workspace = null;
      state.table = null;
      state.edge = null;
    },
    selectTable(state, action: PayloadAction<string | null>) {
      state.table = action.payload;
      // The panel shows one thing at a time, and leaving a stale edge selected
      // means the map highlights a relationship nobody asked about.
      state.edge = null;
    },
    selectEdge(state, action: PayloadAction<string | null>) {
      state.edge = action.payload;
      if (action.payload) state.table = null;
      // Opening a relationship with the panel shut would settle the claim
      // somewhere the reviewer cannot see it.
      if (action.payload) state.mapPaneOpen = true;
    },
    setSearch(state, action: PayloadAction<string>) {
      state.search = action.payload;
    },
    setReviewer(state, action: PayloadAction<string>) {
      state.reviewer = action.payload;
    },
    startJob(state, action: PayloadAction<{ id: string; workspace: string }>) {
      state.runningJobId = action.payload.id;
      state.runningWorkspace = action.payload.workspace;
      state.finishedJob = null;
    },
    /** Track a run this tab did not start — separate from `startJob` because
     *  adopting one must not clear the summary of the last run it did. */
    adoptJob(state, action: PayloadAction<{ id: string; workspace: string }>) {
      state.runningJobId = action.payload.id;
      state.runningWorkspace = action.payload.workspace;
    },
    finishJob(state, action: PayloadAction<Job>) {
      state.runningJobId = null;
      state.runningWorkspace = null;
      state.finishedJob = action.payload;
    },
    dismissFinishedJob(state) {
      state.finishedJob = null;
    },
    setView(state, action: PayloadAction<UiState["view"]>) {
      state.view = action.payload;
    },
    setMapPaneOpen(state, action: PayloadAction<boolean>) {
      state.mapPaneOpen = action.payload;
    },
  },
});

export const {
  selectWorkspace,
  clearWorkspace,
  selectTable,
  setSearch,
  setReviewer,
  startJob,
  adoptJob,
  finishJob,
  dismissFinishedJob,
  setView,
  setMapPaneOpen,
  selectEdge,
} = uiSlice.actions;
export default uiSlice.reducer;

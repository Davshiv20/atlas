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
  /**
   * The last run that ended, kept after `runningJobId` clears.
   *
   * Without it the status vanished the instant a job finished, so a run that
   * took ten minutes ended with no acknowledgement that it had done anything.
   */
  finishedJob: Job | null;
  view: "workspace" | "map" | "questions" | "sources";
}

const initialState: UiState = {
  workspace: null,
  table: null,
  search: "",
  // No auth yet, so the reviewer is self-declared. It is still recorded on
  // every verdict, because an approval with no name attached is not a review.
  reviewer: "you",
  runningJobId: null,
  finishedJob: null,
  view: "workspace",
};

const uiSlice = createSlice({
  name: "ui",
  initialState,
  reducers: {
    selectWorkspace(state, action: PayloadAction<string>) {
      state.workspace = action.payload;
      state.table = null;
    },
    selectTable(state, action: PayloadAction<string | null>) {
      state.table = action.payload;
    },
    setSearch(state, action: PayloadAction<string>) {
      state.search = action.payload;
    },
    setReviewer(state, action: PayloadAction<string>) {
      state.reviewer = action.payload;
    },
    startJob(state, action: PayloadAction<string>) {
      state.runningJobId = action.payload;
      state.finishedJob = null;
    },
    /** Track a run this tab did not start — separate from `startJob` because
     *  adopting one must not clear the summary of the last run it did. */
    adoptJob(state, action: PayloadAction<string>) {
      state.runningJobId = action.payload;
    },
    finishJob(state, action: PayloadAction<Job>) {
      state.runningJobId = null;
      state.finishedJob = action.payload;
    },
    dismissFinishedJob(state) {
      state.finishedJob = null;
    },
    setView(state, action: PayloadAction<UiState["view"]>) {
      state.view = action.payload;
    },
  },
});

export const {
  selectWorkspace,
  selectTable,
  setSearch,
  setReviewer,
  startJob,
  adoptJob,
  finishJob,
  dismissFinishedJob,
  setView,
} = uiSlice.actions;
export default uiSlice.reducer;

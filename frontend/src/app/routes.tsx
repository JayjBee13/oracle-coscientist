import { Route, Routes } from "react-router-dom";

import { ComparePage } from "../pages/ComparePage";
import { HowItWorksPage } from "../pages/HowItWorksPage";
import { HypothesisPage } from "../pages/HypothesisPage";
import { NewRunPage } from "../pages/NewRunPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { RunDetailPage } from "../pages/RunDetailPage";
import { RunsListPage } from "../pages/RunsListPage";

/**
 * The information architecture from contract C6. A run id is the unit of
 * navigation: every view of a run is a URL you can bookmark, reload and share
 * with yourself tomorrow. The previous UI kept all of this in component state.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<RunsListPage />} />
      <Route path="/runs/:id" element={<RunDetailPage />} />
      <Route path="/runs/:id/hypotheses/:hid" element={<HypothesisPage />} />
      <Route path="/new" element={<NewRunPage />} />
      <Route path="/how-it-works" element={<HowItWorksPage />} />
      <Route path="/compare" element={<ComparePage />} />
      <Route path="/compare/:a/:b" element={<ComparePage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

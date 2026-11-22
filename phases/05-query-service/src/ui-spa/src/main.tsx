import React, { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Layout from "./components/Layout";
import { ProRoute } from "./components/ProRoute";
import { OrbitThemeProvider } from "./components/OrbitTheme";
import "./styles/app.css";

const Home = lazy(() => import("./pages/Home"));
const Uploads = lazy(() => import("./pages/Uploads"));
const Explore = lazy(() => import("./pages/Explore"));
const Admin = lazy(() => import("./pages/Admin"));
const Robots = lazy(() => import("./pages/Robots"));
const Obs = lazy(() => import("./pages/Obs"));
const AuthCallback = lazy(() => import("./pages/AuthCallback"));
const Help = lazy(() => import("./pages/Help"));
const NotFound = lazy(() => import("./pages/NotFound"));

const root = createRoot(document.getElementById("root")!);
const queryClient = new QueryClient();
root.render(
  <React.StrictMode>
    <OrbitThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter basename="/ui">
          <Suspense fallback={<div className="p-4 text-sm text-muted">Loading…</div>}>
            <Routes>
              <Route path="auth/callback" element={<AuthCallback />} />
              <Route path="/" element={<Layout />}>
                <Route index element={<Home />} />
                <Route path="uploads" element={<Uploads />} />
                <Route path="episodes" element={<ProRoute route="episodes" feature="Episodes" />} />
                <Route path="datasets" element={<ProRoute route="datasets" feature="Datasets" />} />
                <Route path="explore" element={<Explore />} />
                <Route path="robots" element={<Robots />} />
                <Route path="admin" element={<Admin />} />
                <Route path="obs" element={<Obs />} />
                <Route path="help" element={<Help />} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </QueryClientProvider>
    </OrbitThemeProvider>
  </React.StrictMode>
);

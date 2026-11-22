import { ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@robot-vault/ui-kit";
import { OrbitThemeProvider } from "../components/OrbitTheme";

/**
 * The providers this SPA's pages actually run under, minus the retry/refetch
 * behaviour that makes tests slow and flaky.
 *
 * A router is included where the robot SPA's equivalent has none: this app is
 * `react-router-dom`-based, its pages read `useSearchParams`, and its toasts
 * render `<Link>`. `MemoryRouter` also lets a test state the URL a page is
 * being entered at, which is exactly what the `?id=` deep-link behaviour needs.
 */
export function renderWithProviders(ui: ReactNode, { route = "/" }: { route?: string } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <OrbitThemeProvider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          <ToastProvider>{ui}</ToastProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </OrbitThemeProvider>
  );
}

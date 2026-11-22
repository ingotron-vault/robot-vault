import { ReactNode } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@robot-vault/ui-kit";
import { RobotProvider } from "../context/RobotContext";
import { ActivityLogProvider } from "../lib/activityLog";

export function renderWithProviders(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RobotProvider>
          <ActivityLogProvider>{ui}</ActivityLogProvider>
        </RobotProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}

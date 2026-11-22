import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

export type UrlFilterPatch = Record<string, string | number | null>;

/** Apply one filter action in one navigation, retaining unrelated query keys. */
export function useUrlFilters(): [URLSearchParams, (patch: UrlFilterPatch) => void] {
  const [params, setParams] = useSearchParams();
  const update = useCallback((patch: UrlFilterPatch) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      for (const [name, value] of Object.entries(patch)) {
        if (value === null || value === "") next.delete(name);
        else next.set(name, String(value));
      }
      return next;
    }, { replace: true });
  }, [setParams]);
  return [params, update];
}

export function numberFilter(params: URLSearchParams, name: string, fallback: number): number {
  const raw = params.get(name);
  const value = raw ? Number(raw) : NaN;
  return Number.isFinite(value) ? value : fallback;
}

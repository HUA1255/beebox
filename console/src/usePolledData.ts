import { useCallback, useEffect, useRef, useState } from "react";

interface PolledData<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  refresh: () => void;
}

/**
 * Run `load` immediately and then every `intervalMs` until unmount. The
 * latest `load` is kept in a ref so pages can pass an inline closure without
 * restarting (or double-firing) the loop on every render.
 */
export function usePolledData<T>(load: () => Promise<T>, intervalMs: number): PolledData<T> {
  const loadRef = useRef(load);
  loadRef.current = load;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);

  const refresh = useCallback(() => {
    setGeneration((value) => value + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const run = async () => {
      try {
        const value = await loadRef.current();
        if (!cancelled) {
          setData(value);
          setError(null);
        }
      } catch (failure) {
        if (!cancelled) {
          setError(failure);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          timer = window.setTimeout(run, intervalMs);
        }
      }
    };

    void run();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [intervalMs, generation]);

  return { data, error, loading, refresh };
}

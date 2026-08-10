/** before a schema is imported. */
export function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-[440px] rounded-[--radius-panel] border border-red/25 bg-red-soft p-5">
        <h2 className="text-panel font-semibold text-red">Could not reach the engine</h2>
        <p className="mt-1.5 text-body text-ink-2">{message}</p>
        <p className="mt-3 text-meta text-ink-3">
          The engine runs as a separate process — check that it is listening on :8000.
        </p>
      </div>
    </div>
  );
}

export function LoadingState({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <p className="text-body text-ink-3">{label}</p>
    </div>
  );
}

/** Shown in the centre pane before a table is chosen. */

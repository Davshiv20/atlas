import { useEffect, useRef } from "react";

/**
 * A modal dialog.
 *
 * Configuration happens in a popup rather than on its own page because a
 * connection is something you set up and dismiss, not a place you navigate to.
 * A full page for a five-field form is what left the sources screen mostly
 * empty.
 */
export function Modal({
  title,
  description,
  onClose,
  children,
}: {
  title: string;
  description?: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    // The page behind must not scroll while a dialog is open, or dismissing it
    // returns you somewhere other than where you were.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/25 px-4 py-16 backdrop-blur-[2px]"
      onMouseDown={(event) => {
        if (!panel.current?.contains(event.target as Node)) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-[560px] rounded-[--radius-panel] border border-line bg-surface shadow-[0_16px_48px_-12px_rgba(34,34,32,0.28)]"
      >
        <header className="flex items-start gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h2 className="display text-title text-ink">{title}</h2>
            {description && <p className="mt-1 text-body text-ink-3">{description}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded-[--radius-control] px-2 py-1 text-body text-ink-3 hover:bg-raised hover:text-ink"
          >
            ✕
          </button>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

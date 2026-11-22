import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Drawer, Modal, ToastProvider, useToast } from "@robot-vault/ui-kit";


function ToastButton() {
  const { toast } = useToast();
  return <button onClick={() => toast("Saved", "success")}>Notify</button>;
}


function ModalHarness({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <Modal open={open} onClose={() => { setOpen(false); onClose(); }} title="Confirm cut">
        <button>First</button>
        <button>Last</button>
      </Modal>
    </>
  );
}

function DrawerHarness({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Inspect upload</button>
      <Drawer open={open} onClose={() => { setOpen(false); onClose(); }} title="Upload details">
        <button>Download</button>
      </Drawer>
    </>
  );
}


describe("ui-kit accessibility contracts", () => {
  it("labels a modal, traps focus, closes on Escape, and restores focus", async () => {
    const onClose = vi.fn();
    render(<ModalHarness onClose={onClose} />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Confirm cut" });
    await waitFor(() => expect(dialog).toHaveFocus());
    const first = screen.getByRole("button", { name: "First" });
    const last = screen.getByRole("button", { name: "Last" });
    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(first).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("announces toast messages through a live status region", () => {
    render(<ToastProvider><ToastButton /></ToastProvider>);
    fireEvent.click(screen.getByRole("button", { name: "Notify" }));
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });

  it("gives drawers a visible close control and restores the opener", async () => {
    const onClose = vi.fn();
    render(<DrawerHarness onClose={onClose} />);
    const opener = screen.getByRole("button", { name: "Inspect upload" });
    opener.focus();
    fireEvent.click(opener);

    expect(screen.getByRole("dialog", { name: "Upload details" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(opener).toHaveFocus());
  });
});

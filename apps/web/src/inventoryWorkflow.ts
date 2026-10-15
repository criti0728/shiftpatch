import type { Ingredient, Task } from "./types";

// Check the reservation owned by this task, not the earliest lot of the entire
// ingredient. A fresh delivery must not silently replace an expired reservation.
export function taskNeedsReallocation(task: Task, inventory: Ingredient[], finishAt: string) {
  const finish = new Date(finishAt).getTime();
  return Object.entries(task.allocations).some(([id, quantity]) => {
    const entry = inventory.find(item => item.id === id);
    if (!entry || entry.on_hand < entry.reserved) return true;
    const rows = task.lot_allocations?.[id];
    // Compatibility with old API responses; current overview migrates all tasks.
    if (!rows) return new Date(entry.expires_at).getTime() <= finish;
    if (rows.reduce((total, row) => total + row.quantity, 0) !== quantity) return true;
    return rows.some(row => {
      const lot = entry.lots?.find(value => value.id === row.lot_id);
      return !lot || lot.quantity < row.quantity || lot.reserved < row.quantity ||
        new Date(lot.expires_at).getTime() <= finish;
    });
  });
}

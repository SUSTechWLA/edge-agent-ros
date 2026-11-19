/* ROS row-major XY geometry shared by the top-down and perspective renderers. */
window.EdgeGrid = (() => {
  const rotations = new WeakMap();
  const validated = new WeakMap();
  const colors = Array.from({length: 101}, (_, value) => {
    const t = value / 100;
    return [57 + 177*t, 87 + 93*t, 94 + 32*t].map(Math.round);
  });
  const unknown = [34, 48, 56];

  function usable(grid, frame) {
    if (!grid || grid.frame !== frame || !["online","stale"].includes(grid.status)) return false;
    if (validated.has(grid)) return validated.get(grid);
    const ok = Number.isInteger(grid.width) && Number.isInteger(grid.height) &&
      grid.width > 0 && grid.width <= 4096 && grid.height > 0 && grid.height <= 4096 &&
      grid.width * grid.height <= 262144 && grid.cells?.length === grid.width * grid.height &&
      Number.isFinite(grid.resolution) && grid.resolution > 0 &&
      Array.isArray(grid.origin) && grid.origin.length === 3 && grid.origin.every(Number.isFinite) &&
      Number.isFinite(grid.origin_z ?? 0) && Array.isArray(grid.cells) &&
      grid.cells.every(value => Number.isInteger(value) && value >= -1 && value <= 100);
    validated.set(grid,ok);
    return ok;
  }
  function rotation(grid) {
    if (!rotations.has(grid)) rotations.set(grid, [Math.cos(grid.origin[2]), Math.sin(grid.origin[2])]);
    return rotations.get(grid);
  }
  function toWorld(grid, col, row) {
    const [c,s] = rotation(grid), k = grid.resolution;
    return [grid.origin[0] + (col*c - row*s)*k, grid.origin[1] + (col*s + row*c)*k];
  }
  function cellAt(grid, x, y) {
    const [c,s] = rotation(grid), dx = x-grid.origin[0], dy = y-grid.origin[1];
    const col = Math.floor((c*dx + s*dy)/grid.resolution), row = Math.floor((-s*dx + c*dy)/grid.resolution);
    if (!(col >= 0 && col < grid.width && row >= 0 && row < grid.height)) return null;
    return {col, row, value:grid.cells[row*grid.width + col]};
  }
  function describe(value) {
    if (value === -1) return "未知";
    if (value === 0) return "空闲";
    if (value === 100) return "占据";
    return "占据概率 " + value + "%";
  }
  return {usable, toWorld, cellAt, describe, color: value => colors[value] || unknown};
})();

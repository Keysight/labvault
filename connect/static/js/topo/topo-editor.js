/**
 * TopoEditor — undo/redo stack for Lab Topology Designer (Phase 4).
 * @see docs/TOPOLOGY_VIEWS_IMPLEMENTATION_PLAN.md §7.3
 */
export class EditHistory {
  constructor(maxDepth = 20) {
    this._stack = [];
    this._pointer = -1;
    this._maxDepth = maxDepth;
    this._onChange = null;
  }

  onStateChange(cb) { this._onChange = cb; }

  push(action) {
    // Discard redo branch
    this._stack = this._stack.slice(0, this._pointer + 1);
    this._stack.push(action);
    if (this._stack.length > this._maxDepth) this._stack.shift();
    this._pointer = this._stack.length - 1;
    this._notify();
  }

  undo() {
    if (!this.canUndo()) return;
    const action = this._stack[this._pointer--];
    if (action.inverse) action.inverse();
    this._notify();
    return action;
  }

  redo() {
    if (!this.canRedo()) return;
    const action = this._stack[++this._pointer];
    if (action.execute) action.execute();
    this._notify();
    return action;
  }

  canUndo() { return this._pointer >= 0; }
  canRedo() { return this._pointer < this._stack.length - 1; }

  clear() {
    this._stack = [];
    this._pointer = -1;
    this._notify();
  }

  _notify() {
    if (this._onChange) this._onChange({ canUndo: this.canUndo(), canRedo: this.canRedo() });
  }
}

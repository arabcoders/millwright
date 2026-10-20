export type LogStreamEvent = { id: number | null; event: string; data: string };

export class LogStreamParser {
  private buffer = '';
  private id: number | null = null;
  private event = '';
  private data: string[] = [];

  feed(chunk: string): LogStreamEvent[] {
    this.buffer += chunk;
    const events: LogStreamEvent[] = [];
    let boundary = this.buffer.indexOf('\n');
    while (boundary >= 0) {
      let line = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) {
        if (this.data.length)
          events.push({ id: this.id, event: this.event || 'message', data: this.data.join('\n') });
        this.id = null;
        this.event = '';
        this.data = [];
      } else if (!line.startsWith(':')) {
        const separator = line.indexOf(':');
        const field = separator < 0 ? line : line.slice(0, separator);
        const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /u, '');
        if (field === 'id' && /^\d+$/u.test(value)) this.id = Number(value);
        else if (field === 'event') this.event = value;
        else if (field === 'data') this.data.push(value);
      }
      boundary = this.buffer.indexOf('\n');
    }
    return events;
  }
}

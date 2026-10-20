import { describe, expect, test } from 'bun:test';
import { LogStreamParser } from '../app/utils/logStream';

describe('LogStreamParser', () => {
  test('parses chunked unicode events', () => {
    const parser = new LogStreamParser();
    expect(parser.feed('id: 7\nevent: log_lines\ndata: {"message":"caf')).toEqual([]);
    expect(parser.feed('é"}\n\n')).toEqual([
      { id: 7, event: 'log_lines', data: '{"message":"café"}' },
    ]);
  });

  test('joins multiline data and ignores comments', () => {
    const parser = new LogStreamParser();
    expect(parser.feed(': keepalive\nid: 8\ndata: first\ndata: second\r\n\r\n')).toEqual([
      { id: 8, event: 'message', data: 'first\nsecond' },
    ]);
  });
});

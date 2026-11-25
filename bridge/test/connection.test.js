import test from 'node:test';
import assert from 'node:assert/strict';
import { calculateBackoff } from '../src/connection.js';

test('connection: backoff sequence bounds', () => {
  // Test attempt 0: base 1000ms + jitter [0..1000)
  for (let i = 0; i < 20; i++) {
    const d0 = calculateBackoff(0);
    assert.ok(d0 >= 1000 && d0 <= 2000, `d0 out of bounds: ${d0}`);
  }

  // Test attempt 1: base 1500ms + jitter [0..1000)
  for (let i = 0; i < 20; i++) {
    const d1 = calculateBackoff(1);
    assert.ok(d1 >= 1500 && d1 <= 2500, `d1 out of bounds: ${d1}`);
  }

  // Test high attempt number: must cap at 60,000 ms (60 seconds)
  for (let i = 0; i < 20; i++) {
    const dHigh = calculateBackoff(20);
    assert.equal(dHigh, 60000);
  }
});

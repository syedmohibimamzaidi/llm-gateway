import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeGatewayKey } from '../src/auth.js'

test('accepts a bare gateway key', () => {
  assert.equal(normalizeGatewayKey('  fixture-secret  '), 'fixture-secret')
})

test('accepts a quoted .env assignment', () => {
  assert.equal(normalizeGatewayKey("GATEWAY_API_KEY='fixture-secret'"), 'fixture-secret')
  assert.equal(normalizeGatewayKey('export GATEWAY_API_KEY="fixture-secret"'), 'fixture-secret')
})

test('does not mistake a provider key assignment for a gateway key', () => {
  assert.equal(normalizeGatewayKey('OPENAI_API_KEY=fixture-secret'), 'OPENAI_API_KEY=fixture-secret')
})

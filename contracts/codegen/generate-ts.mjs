/**
 * Generate TypeScript definitions from contracts/canonical-event.schema.json.
 * Usage: node contracts/codegen/generate-ts.mjs [--check]
 */
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const __dirname = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(__dirname, '../..')
const schemaPath = resolve(repoRoot, 'contracts/canonical-event.schema.json')
const outPath = resolve(
  repoRoot,
  'apps/desktop/src/renderer/src/sdk/generated/canonical-event.ts'
)

const requireFromDesktop = createRequire(resolve(repoRoot, 'apps/desktop/package.json'))
const { compile } = requireFromDesktop('json-schema-to-typescript')

const raw = JSON.parse(await readFile(schemaPath, 'utf-8'))
const ts = await compile(raw, 'RuntimeEvent', {
  bannerComment:
    '/* eslint-disable */\n/**\n * AUTO-GENERATED from contracts/canonical-event.schema.json.\n * Do not edit manually — run `pnpm contracts:sync` to regenerate.\n */',
  additionalProperties: false,
  strictIndexSignatures: false,
  style: {
    singleQuote: true,
    semi: false,
    printWidth: 100
  }
})

const checkOnly = process.argv.includes('--check')
if (checkOnly) {
  const existing = await readFile(outPath, 'utf-8').catch(() => null)
  if (existing !== ts) {
    console.error(`Generated types out of sync: ${outPath}. Run \`pnpm contracts:sync\`.`)
    process.exit(1)
  }
  console.log('Generated canonical-event.ts is up to date.')
} else {
  await mkdir(dirname(outPath), { recursive: true })
  await writeFile(outPath, ts, 'utf-8')
  console.log(`Generated ${outPath}`)
}

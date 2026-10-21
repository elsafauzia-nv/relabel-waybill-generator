// Populate a captured PRINTER:PRINT job's template with the installed nv-label-printer agent's
// own label.js#generateZpl, mirroring server/services/printer.js#createJob.
//
// usage: node render_with_agent.js <path/to/agent/server/helpers/label.js> < job.json
// stdout: JSON {zpl, path}  |  exit 2 + stderr message on failure
'use strict'

const labelPath = process.argv[2]
if (!labelPath) {
  console.error('missing label.js path')
  process.exit(2)
}
const label = require(labelPath)

let input = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', chunk => (input += chunk))
process.stdin.on('end', () => {
  let job
  try {
    job = JSON.parse(input)
  } catch (e) {
    console.error(`invalid job JSON: ${e.message}`)
    process.exit(2)
  }

  // createJob(): precedence is job.zpl > template_id (agent-local store) > template_string
  let zpl
  let path
  try {
    if (job.zpl) {
      zpl = job.zpl
      path = 'job.zpl'
    } else if (job.system_id !== null && job.template_id > 0) {
      throw new Error(
        'job references an agent-local template_id; its template is not in the payload'
      )
    } else if (job.system_id !== null && job.template_string !== null) {
      zpl = label.generateZpl(job.template_string, job)
      path = 'generateZpl(template_string)'
    } else {
      throw new Error('Can not process job. Missing system id, zpl or template id.')
    }
  } catch (e) {
    console.error(`agent failed to generate ZPL: ${e && e.message ? e.message : e}`)
    process.exit(2)
  }
  if (!zpl) {
    console.error('agent failed to generate ZPL: empty result')
    process.exit(2)
  }

  // The full string; the caller picks the encoding (the agent's printJob truncates each
  // character to 8 bits, which the caller reports instead of copying).
  process.stdout.write(JSON.stringify({ zpl, path }))
})

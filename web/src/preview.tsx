import { createContext, useContext } from 'react'

export interface FileRef { path: string; galaxy?: string }

/** Opens a file in the right-hand preview panel. */
export const PreviewContext = createContext<(f: FileRef) => void>(() => {})
export const usePreview = () => useContext(PreviewContext)

export function rawUrl(path: string, galaxy?: string, download = false) {
  const q = new URLSearchParams({ path })
  if (galaxy) q.set('galaxy', galaxy)
  if (download) q.set('download', '1')
  return `api/raw?${q}`
}

const IMAGE = /\.(png|jpe?g|gif|webp|svg|bmp|avif)(\?.*)?$/i
const VIDEO = /\.(mp4|webm|mov|m4v|ogv)(\?.*)?$/i
export const isImage = (u: string) => IMAGE.test(u)
export const isVideo = (u: string) => VIDEO.test(u)

/** An absolute path on some galaxy, as agents write them in reports. */
export const FILE_PATH = /(?<![\w/.:~-])\/(?:[\w.@+~一-鿿-]+\/)*[\w@+~一-鿿-][\w.@+~一-鿿-]*\.[A-Za-z0-9]{1,8}(?![\w/])/g
export const looksLikePath = (s: string) => /^\/[^\s]*[^\s/.,;:)]$/.test(s) && !s.startsWith('//')

export function resolvePath(src: string, base?: string) {
  if (src.startsWith('/')) return src
  if (!base) return src
  const parts = base.split('/').slice(0, -1)
  for (const seg of src.split('/')) {
    if (seg === '..') parts.pop()
    else if (seg !== '.' && seg !== '') parts.push(seg)
  }
  return parts.join('/') || '/'
}

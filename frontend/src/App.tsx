import { Routes, Route } from 'react-router-dom'
import { Dashboard } from './Dashboard'
import { CryptoPage } from './components/CryptoPage'

function App() {
  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/crypto" element={<CryptoPage />} />
    </Routes>
  )
}

export default App

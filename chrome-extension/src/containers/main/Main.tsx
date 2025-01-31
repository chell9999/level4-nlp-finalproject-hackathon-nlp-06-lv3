import { useRecoilValue } from "recoil"
import Header from "@/containers/main/header/Header"
import ReportsContainers from "@/containers/main/reports/ReportsContainers"
import viewState from "@/states/viewState"
import ReportPage from "@/containers/main/reports/ReportPage"

export default function Main() {
  const view = useRecoilValue(viewState)

  return (
    <div className="flex flex-col justify-center w-full">
      <Header />
      <div className="p-7">
        {view.type === "home" && <ReportsContainers />}
        {view.type === "report" && <ReportPage />}
      </div>
    </div>
  )
}

import React, { useRef, useState } from "react";
import styles from "../css/ImageHandles.module.css";

const ImageDragDrop = ({ files, setFiles }) => {
  const dragItem = useRef();
  const dragOverItem = useRef();
  const [activePreview, setActivePreview] = useState(null);

  const handleDragStart = (index) => (dragItem.current = index);
  const handleDragEnter = (index) => (dragOverItem.current = index);
  const handleDragEnd = () => {
    const newList = [...files];
    const dragged = newList[dragItem.current];
    newList.splice(dragItem.current, 1);
    newList.splice(dragOverItem.current, 0, dragged);
    dragItem.current = null;
    dragOverItem.current = null;
    setFiles(newList);
  };

  const removeImage = (index) => {
    const newFiles = [...files];
    if (newFiles[index]?.preview && newFiles[index].preview.startsWith("blob:")) {
      URL.revokeObjectURL(newFiles[index].preview);
    }
    newFiles.splice(index, 1);
    setFiles(newFiles);
  };

  return (
    <>
      <div className={styles.imageDragDropContainer}>
        {files.map((file, idx) => (
          <div
            key={file.name + idx}
            draggable
            onDragStart={() => handleDragStart(idx)}
            onDragEnter={() => handleDragEnter(idx)}
            onDragEnd={handleDragEnd}
            onDragOver={(e) => e.preventDefault()}
            onDoubleClick={() => setActivePreview(file)}
            className={styles.imageCard}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setActivePreview(file);
              }
            }}
          >
            <img src={file.preview} alt={file.name} className={styles.imagePreview} />
            <div className={styles.imageName}>{file.name}</div>

            <div className={styles.imageIndex}>{idx + 1}</div>

            <button onClick={() => removeImage(idx)} className={styles.removeButton} type="button">
              x
            </button>
          </div>
        ))}
      </div>

      {activePreview && (
        <div
          className={styles.previewOverlay}
          onClick={() => setActivePreview(null)}
          role="dialog"
          aria-modal="true"
        >
          <div className={styles.previewModal} onClick={(e) => e.stopPropagation()}>
            <div className={styles.previewHeader}>
              <div className={styles.previewTitle}>{activePreview.name}</div>
              <button
                type="button"
                className={styles.previewClose}
                onClick={() => setActivePreview(null)}
              >
                Close
              </button>
            </div>
            <div className={styles.previewBody}>
              <img src={activePreview.preview} alt={activePreview.name} />
            </div>
          </div>
        </div>
      )}
    </>
  );
};

export default ImageDragDrop;
